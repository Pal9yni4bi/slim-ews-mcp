"""MCP tool implementations.

Safety invariants, enforced in code (not configurable):
- reply/forward/move/delete are two-step: with confirm=False (the default)
  they only return a preview and never touch the mailbox; the actual send/
  move/delete code is unreachable unless confirm=True is passed explicitly.
- delete is always a soft delete (move to Deleted Items), never a hard delete.
- logs contain operation metadata only - never bodies, subjects or credentials.
"""

from __future__ import annotations

import functools
import html as html_escape
import logging
import re
from itertools import islice

from exchangelib import HTMLBody
from exchangelib.errors import EWSError

from config import load_config
from ews_client import (
    EwsToolError,
    friendly_ews_error,
    get_account,
    get_message_by_id,
    resolve_folder,
    walk_mail_folders,
)
from html_to_md import body_to_markdown, make_snippet

try:
    from mcp.server.fastmcp.exceptions import ToolError
except ImportError:  # pragma: no cover - fallback for older SDK layouts
    class ToolError(Exception):
        pass

logger = logging.getLogger("ews_mcp")

BODY_CHAR_LIMIT = 20000
SNIPPET_LENGTH = 200

PREVIEW_NOTE = (
    "PREVIEW ONLY - nothing was executed. Show this preview to the user and "
    "wait for their explicit approval. Only then call the same tool again "
    "with the same arguments plus confirm=true."
)

_LIST_FIELDS = ("id", "datetime_received", "sender", "subject", "is_read",
                "has_attachments", "text_body")


def _tool_guard(fn):
    """Convert internal exceptions into short, readable tool errors."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:
            logger.info("%s failed: %s", fn.__name__, type(exc).__name__)
            raise ToolError(friendly_ews_error(exc)) from exc

    return wrapper


# --- formatting helpers --------------------------------------------------

def _mailbox_str(mailbox) -> str:
    if mailbox is None:
        return ""
    name = (mailbox.name or "").strip()
    email = mailbox.email_address or ""
    if name and name.lower() != email.lower():
        return f"{name} <{email}>"
    return email


def _format_dt(dt) -> str:
    if dt is None:
        return ""
    try:
        dt = dt.astimezone()  # convert to local time
    except Exception:
        pass
    return dt.isoformat(timespec="minutes")


def _text_to_html(text: str) -> str:
    """Escape plain text and preserve line breaks for an HTML mail body."""
    return html_escape.escape(text).replace("\n", "<br>\n")


def _prefixed_subject(subject: str | None, prefixes: tuple[str, ...], prefix: str) -> str:
    subject = subject or ""
    pattern = r"^\s*(" + "|".join(prefixes) + r")\s*:"
    if re.match(pattern, subject, re.IGNORECASE):
        return subject
    return f"{prefix}{subject}"


def _parse_recipients(to) -> list[str]:
    if isinstance(to, str):
        parts = [p.strip() for p in re.split(r"[,;]", to)]
    else:
        parts = [str(p).strip() for p in to]
    recipients = [p for p in parts if p]
    if not recipients:
        raise EwsToolError("No recipients given.")
    for r in recipients:
        if "@" not in r:
            raise EwsToolError(f"Invalid recipient address: {r!r}")
    return recipients


def _reply_recipients(item, reply_all: bool) -> tuple[list[str], list[str]]:
    """Best-effort preview of reply recipients (the server computes the
    authoritative list when the reply is sent)."""
    me = load_config().email.lower()
    seen: set[str] = set()

    def _add(bucket: list[str], mailbox) -> None:
        email = (getattr(mailbox, "email_address", None) or "").lower()
        if not email or email == me or email in seen:
            return
        seen.add(email)
        bucket.append(_mailbox_str(mailbox))

    to: list[str] = []
    cc: list[str] = []
    for mailbox in (item.reply_to or [item.sender or item.author]):
        _add(to, mailbox)
    if reply_all:
        for mailbox in item.to_recipients or []:
            _add(to, mailbox)
        for mailbox in item.cc_recipients or []:
            _add(cc, mailbox)
    if not to:  # replying to yourself
        to = [_mailbox_str(item.sender or item.author)]
    return to, cc


def _message_brief(item) -> dict:
    return {
        "id": item.id,
        "subject": item.subject or "",
        "from": _mailbox_str(item.sender or item.author),
        "date": _format_dt(item.datetime_received),
    }


# --- tools ---------------------------------------------------------------

@_tool_guard
def list_messages(folder: str = "inbox", limit: int = 25,
                  unread_only: bool = False, query: str | None = None) -> dict:
    """List message metadata from a mail folder - never the bodies.

    Returns messages newest first: id, date, sender, subject, read flag,
    attachments flag and a ~200-character text snippet. Use get_message to
    read a specific email.

    Args:
        folder: "inbox", "sent", "drafts", "trash", "junk", a display-name
            path like "Inbox/Projects", or a folder id from list_folders.
        limit: Maximum number of messages (a server-side hard cap applies).
        unread_only: Return only unread messages.
        query: Optional Exchange search query (AQS syntax), e.g.
            'from:alice subject:report' or 'hasattachment:true'.
    """
    cfg = load_config()
    limit = max(1, min(limit, cfg.max_list_limit))
    target = resolve_folder(folder)

    def _build(fields):
        if query:
            qs = target.filter(query)
        else:
            qs = target.all()
            if unread_only:
                qs = qs.filter(is_read=False)
        return qs.order_by("-datetime_received").only(*fields)

    # AQS query strings cannot be combined with other filters, so with both
    # query and unread_only we over-fetch and drop read messages client-side.
    fetch_limit = min(limit * 3, cfg.max_list_limit * 3) if (query and unread_only) else limit
    try:
        items = list(islice(_build(_LIST_FIELDS), fetch_limit))
        snippets = True
    except EWSError:
        # Older Exchange versions do not support the TextBody property.
        items = list(islice(_build([f for f in _LIST_FIELDS if f != "text_body"]), fetch_limit))
        snippets = False

    rows = []
    for item in items:
        if unread_only and query and item.is_read:
            continue
        rows.append({
            "id": item.id,
            "date": _format_dt(item.datetime_received),
            "from": _mailbox_str(item.sender or getattr(item, "author", None)),
            "subject": item.subject or "",
            "read": bool(item.is_read),
            "attachments": bool(item.has_attachments),
            "snippet": make_snippet(getattr(item, "text_body", None), SNIPPET_LENGTH) if snippets else "",
        })
        if len(rows) >= limit:
            break

    logger.info("list_messages folder=%s returned=%d unread_only=%s query=%s",
                target.name, len(rows), unread_only, "yes" if query else "no")
    return {"folder": target.name, "count": len(rows), "messages": rows}


@_tool_guard
def get_message(message_id: str, include_quoted: bool = False) -> dict:
    """Read one email in full: headers, Markdown body, attachment list.

    HTML bodies are converted to Markdown to save tokens. Quoted thread
    history is trimmed by default - pass include_quoted=true to keep it.
    Attachments are listed with names and sizes only; contents are never
    downloaded.

    Args:
        message_id: Message id from list_messages.
        include_quoted: Keep the quoted history of the thread in the body.
    """
    item = get_message_by_id(message_id)

    body = item.body
    is_html = isinstance(body, HTMLBody)
    markdown, trimmed = body_to_markdown(str(body) if body else "", is_html, include_quoted)
    if len(markdown) > BODY_CHAR_LIMIT:
        markdown = markdown[:BODY_CHAR_LIMIT].rstrip() + "\n\n*[Body truncated]*"
    if trimmed:
        markdown += ("\n\n*[Quoted history trimmed - call get_message with "
                     "include_quoted=true to see the full thread.]*")

    attachments = [
        {
            "name": att.name,
            "size": att.size,
            "content_type": getattr(att, "content_type", None),
            "inline": bool(getattr(att, "is_inline", False)),
        }
        for att in item.attachments or []
    ]

    logger.info("get_message id=%s include_quoted=%s", message_id[-16:], include_quoted)
    return {
        "id": item.id,
        "date": _format_dt(item.datetime_received),
        "from": _mailbox_str(item.sender or item.author),
        "to": [_mailbox_str(r) for r in item.to_recipients or []],
        "cc": [_mailbox_str(r) for r in item.cc_recipients or []],
        "subject": item.subject or "",
        "read": bool(item.is_read),
        "quoted_history_trimmed": trimmed,
        "body_markdown": markdown,
        "attachments": attachments,
    }


@_tool_guard
def reply_email(message_id: str, body: str, reply_all: bool = False,
                confirm: bool = False) -> dict:
    """Reply to an email (optionally reply-all).

    SAFETY - two-step confirmation is mandatory. With confirm=false (the
    default) NOTHING is sent: the tool returns a preview with recipients,
    subject and the exact reply text. Show that preview to the user, wait
    for their explicit approval, and only then call the tool again with
    confirm=true. Never set confirm=true without the user's approval.

    The Exchange server appends the quoted original message and preserves
    threading automatically.

    Args:
        message_id: Id of the message to reply to.
        body: Reply text (plain text; line breaks are preserved).
        reply_all: Reply to all recipients instead of only the sender.
        confirm: Must be true to actually send. Default false = preview only.
    """
    if not body or not body.strip():
        raise ToolError("Reply body is empty.")
    item = get_message_by_id(message_id, require_message=True)
    action = "reply_all" if reply_all else "reply"
    subject = _prefixed_subject(item.subject, ("re", "отв"), "Re: ")
    to, cc = _reply_recipients(item, reply_all)

    if not confirm:
        return {
            "status": "preview",
            "action": action,
            "original": _message_brief(item),
            "to": to,
            "cc": cc,
            "subject": subject,
            "body": body,
            "note": PREVIEW_NOTE,
        }

    # --- past the confirmation gate: the only code path that can send ---
    html_body = HTMLBody(_text_to_html(body))
    if reply_all:
        draft = item.create_reply_all(subject, html_body)
    else:
        draft = item.create_reply(subject, html_body)
    draft.send()

    logger.info("%s sent for id=%s", action, message_id[-16:])
    return {"status": "sent", "action": action, "to": to, "cc": cc, "subject": subject}


@_tool_guard
def forward_email(message_id: str, to: str | list[str], body: str = "",
                  confirm: bool = False) -> dict:
    """Forward an email; original attachments are included by the server.

    SAFETY - two-step confirmation is mandatory. With confirm=false (the
    default) NOTHING is sent: the tool returns a preview with recipients,
    subject and the comment text. Show that preview to the user, wait for
    their explicit approval, and only then call the tool again with
    confirm=true. Never set confirm=true without the user's approval.

    Args:
        message_id: Id of the message to forward.
        to: Recipient address(es) - a list or a comma/semicolon-separated string.
        body: Optional comment placed above the forwarded message.
        confirm: Must be true to actually send. Default false = preview only.
    """
    recipients = _parse_recipients(to)
    item = get_message_by_id(message_id, require_message=True)
    subject = _prefixed_subject(item.subject, ("fw", "fwd", "пересл"), "Fw: ")

    if not confirm:
        return {
            "status": "preview",
            "action": "forward",
            "original": _message_brief(item),
            "to": recipients,
            "subject": subject,
            "body": body,
            "attachments_included": bool(item.has_attachments),
            "note": PREVIEW_NOTE,
        }

    # --- past the confirmation gate: the only code path that can send ---
    html_body = HTMLBody(_text_to_html(body)) if body else HTMLBody("")
    draft = item.create_forward(subject, html_body, to_recipients=recipients)
    draft.send()

    logger.info("forward sent for id=%s recipients=%d", message_id[-16:], len(recipients))
    return {"status": "sent", "action": "forward", "to": recipients, "subject": subject}


@_tool_guard
def move_message(message_id: str, target_folder: str, confirm: bool = False) -> dict:
    """Move a message to another folder.

    SAFETY - two-step confirmation is mandatory. With confirm=false (the
    default) nothing is moved: the tool returns a preview of what would move
    where. Show it to the user, wait for approval, then call again with
    confirm=true. Note: the message id changes after a move.

    Args:
        message_id: Id of the message to move.
        target_folder: Alias (inbox, trash, ...), path like "Inbox/Projects",
            or a folder id from list_folders.
        confirm: Must be true to actually move. Default false = preview only.
    """
    item = get_message_by_id(message_id)
    target = resolve_folder(target_folder)

    if not confirm:
        return {
            "status": "preview",
            "action": "move",
            "message": _message_brief(item),
            "target_folder": target.name,
            "note": PREVIEW_NOTE,
        }

    # --- past the confirmation gate ---
    item.move(target)
    logger.info("move id=%s -> folder=%s", message_id[-16:], target.name)
    return {"status": "moved", "target_folder": target.name, "new_id": item.id}


@_tool_guard
def delete_message(message_id: str, confirm: bool = False) -> dict:
    """Delete a message - SOFT delete only (moves it to Deleted Items).

    Hard/permanent deletion is not implemented in this server at all; a
    deleted message can always be recovered from Deleted Items.

    SAFETY - two-step confirmation is mandatory. With confirm=false (the
    default) nothing is deleted: the tool returns a preview of what would be
    deleted. Show it to the user, wait for approval, then call again with
    confirm=true.

    Args:
        message_id: Id of the message to delete.
        confirm: Must be true to actually delete. Default false = preview only.
    """
    item = get_message_by_id(message_id)

    if not confirm:
        return {
            "status": "preview",
            "action": "soft_delete",
            "message": _message_brief(item),
            "target_folder": "Deleted Items",
            "note": PREVIEW_NOTE,
        }

    # --- past the confirmation gate ---
    item.move_to_trash()
    logger.info("soft-delete id=%s", message_id[-16:])
    return {"status": "deleted", "detail": "Moved to Deleted Items (recoverable).",
            "new_id": item.id}


@_tool_guard
def mark_read(message_id: str, read: bool = True) -> dict:
    """Mark a message as read or unread. Safe, needs no confirmation.

    Args:
        message_id: Id of the message.
        read: True to mark as read, false to mark as unread.
    """
    item = get_message_by_id(message_id)
    item.is_read = read
    item.save(update_fields=["is_read"])
    logger.info("mark_read id=%s read=%s", message_id[-16:], read)
    return {"status": "ok", "id": item.id, "read": read}


@_tool_guard
def list_folders() -> dict:
    """List the mail folder tree with ids, unread and total counts.

    Use the returned names/ids with the folder arguments of list_messages
    and move_message. Aliases inbox, sent, drafts, trash, junk and outbox
    always work regardless of the mailbox language.
    """
    account = get_account()
    root_id = account.msg_folder_root.id
    nodes: dict[str, dict] = {}
    parents: dict[str, str] = {}
    for folder in walk_mail_folders(account):
        nodes[folder.id] = {
            "name": folder.name,
            "id": folder.id,
            "unread": folder.unread_count or 0,
            "total": folder.total_count or 0,
            "children": [],
        }
        parents[folder.id] = folder.parent_folder_id.id if folder.parent_folder_id else root_id

    roots = []
    for folder_id, node in nodes.items():
        parent = nodes.get(parents.get(folder_id))
        if parent is not None:
            parent["children"].append(node)
        else:
            roots.append(node)
    for node in nodes.values():
        node["children"].sort(key=lambda n: n["name"].lower())
    roots.sort(key=lambda n: n["name"].lower())

    logger.info("list_folders returned=%d", len(nodes))
    return {"folders": roots}


ALL_TOOLS = (
    list_messages,
    get_message,
    reply_email,
    forward_email,
    move_message,
    delete_message,
    mark_read,
    list_folders,
)


def register(mcp) -> None:
    """Register all tools on a FastMCP server instance."""
    for tool in ALL_TOOLS:
        mcp.tool()(tool)
