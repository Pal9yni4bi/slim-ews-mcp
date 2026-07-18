"""Exchange connection layer built on exchangelib.

Responsibilities: build the Account from config (explicit endpoint, no
autodiscover unless requested), resolve folder specs to folder objects,
fetch messages by id, and translate EWS/network errors into short
human-readable messages instead of tracebacks.
"""

from __future__ import annotations

import logging

import requests.exceptions
from exchangelib import (
    BASIC,
    DELEGATE,
    DIGEST,
    GSSAPI,
    IMPERSONATION,
    NTLM,
    SSPI,
    Account,
    Configuration,
    Credentials,
    Message,
)
from exchangelib.errors import (
    ErrorAccessDenied,
    ErrorFolderNotFound,
    ErrorInvalidIdMalformed,
    ErrorItemNotFound,
    ErrorNonExistentMailbox,
    EWSError,
    RateLimitError,
    TransportError,
    UnauthorizedError,
)
from exchangelib.folders import Folder
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter

from config import Config, load_config

logger = logging.getLogger("ews_mcp")

_AUTH_TYPE_MAP = {
    "basic": BASIC,
    "ntlm": NTLM,
    "digest": DIGEST,
    "gssapi": GSSAPI,
    "sspi": SSPI,
}

_ACCESS_TYPE_MAP = {
    "delegate": DELEGATE,
    "impersonation": IMPERSONATION,
}

# Aliases for distinguished folders. They resolve by folder role, not display
# name, so they work regardless of the mailbox language.
_FOLDER_ALIASES = {
    "inbox": "inbox",
    "sent": "sent",
    "sent items": "sent",
    "drafts": "drafts",
    "outbox": "outbox",
    "trash": "trash",
    "deleted": "trash",
    "deleted items": "trash",
    "junk": "junk",
    "spam": "junk",
}


class EwsToolError(Exception):
    """A tool-level error with a message safe to show to the model/user."""


_account: Account | None = None


def get_account() -> Account:
    """Build (once) and return the exchangelib Account."""
    global _account
    if _account is not None:
        return _account

    cfg = load_config()
    _apply_protocol_settings(cfg)

    credentials = Credentials(username=cfg.username, password=cfg.password)
    access_type = _ACCESS_TYPE_MAP[cfg.access_type]

    if cfg.autodiscover:
        _account = Account(
            primary_smtp_address=cfg.email,
            credentials=credentials,
            autodiscover=True,
            access_type=access_type,
        )
    else:
        configuration = Configuration(
            service_endpoint=cfg.endpoint,
            credentials=credentials,
            auth_type=_AUTH_TYPE_MAP[cfg.auth_type] if cfg.auth_type else None,
        )
        _account = Account(
            primary_smtp_address=cfg.email,
            config=configuration,
            autodiscover=False,
            access_type=access_type,
        )
    logger.info("account initialized (endpoint=%s, autodiscover=%s)",
                cfg.endpoint or "-", cfg.autodiscover)
    return _account


def _apply_protocol_settings(cfg: Config) -> None:
    BaseProtocol.TIMEOUT = cfg.timeout
    if cfg.insecure_tls:
        logger.warning(
            "EWS_INSECURE_TLS=true: TLS certificate verification is DISABLED. "
            "The connection is vulnerable to man-in-the-middle attacks. "
            "Prefer REQUESTS_CA_BUNDLE with your internal CA certificate."
        )
        BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter


def friendly_ews_error(exc: Exception) -> str:
    """Map an exception from exchangelib/requests to a short readable message."""
    if isinstance(exc, EwsToolError):
        return str(exc)
    if isinstance(exc, (UnauthorizedError, ErrorAccessDenied)):
        return (
            "Authentication failed or access denied. Check EWS_USERNAME, "
            "EWS_PASSWORD and EWS_AUTH_TYPE (for NTLM use DOMAIN\\username)."
        )
    if isinstance(exc, ErrorNonExistentMailbox):
        return "Mailbox not found. Check EWS_EMAIL (must be the primary SMTP address)."
    if isinstance(exc, (ErrorItemNotFound, ErrorInvalidIdMalformed)):
        return (
            "Message not found: the id is invalid or the message was moved or "
            "deleted (ids change when a message moves). Re-run list_messages "
            "to get fresh ids."
        )
    if isinstance(exc, ErrorFolderNotFound):
        return "Folder not found on the server."
    if isinstance(exc, RateLimitError):
        return "The Exchange server is throttling requests. Wait a bit and retry."
    if isinstance(exc, requests.exceptions.SSLError):
        return (
            "TLS certificate verification failed. If the server uses an "
            "internal CA, set REQUESTS_CA_BUNDLE to your CA bundle file "
            "(see README). Do not disable verification unless you must."
        )
    if isinstance(exc, requests.exceptions.Timeout):
        return "EWS request timed out. Check connectivity or raise EWS_TIMEOUT."
    if isinstance(exc, (TransportError, requests.exceptions.ConnectionError, ConnectionError)):
        return (
            "Cannot reach the EWS endpoint. Check EWS_ENDPOINT, network/VPN "
            f"connectivity and TLS settings. Details: {_short(exc)}"
        )
    if isinstance(exc, EWSError):
        return f"EWS error ({type(exc).__name__}): {_short(exc)}"
    return f"Unexpected error ({type(exc).__name__}): {_short(exc)}"


def _short(exc: Exception, limit: int = 300) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:limit] + ("…" if len(text) > limit else "")


def resolve_folder(spec: str):
    """Resolve a folder spec to a folder object.

    Accepted forms:
    - alias of a distinguished folder: inbox, sent, drafts, trash, junk, outbox
      (language-independent);
    - a path of display names relative to the mailbox root, e.g.
      "Inbox/Projects/2026" (case-insensitive, separator "/");
    - an exact folder id as returned by list_folders.
    """
    account = get_account()
    spec = (spec or "").strip()
    if not spec:
        raise EwsToolError("Folder is not specified.")

    alias = _FOLDER_ALIASES.get(spec.lower())
    if alias is not None:
        return getattr(account, alias)

    if "/" in spec:
        return _resolve_path(account, spec)

    # A bare name: try a top-level folder match first, then an id lookup.
    folder = _find_child(account.msg_folder_root, spec)
    if folder is not None:
        return folder
    folder = _find_by_id(account, spec)
    if folder is not None:
        return folder
    raise EwsToolError(
        f"Folder not found: {spec!r}. Use an alias (inbox, sent, drafts, "
        "trash, junk), a path like 'Inbox/Subfolder', or an id from list_folders."
    )


def _resolve_path(account: Account, path: str):
    current = account.msg_folder_root
    for part in [p for p in path.split("/") if p.strip()]:
        child = _find_child(current, part)
        if child is None:
            raise EwsToolError(
                f"Folder path not found: {path!r} (no subfolder named {part!r})."
            )
        current = child
    return current


def _find_child(parent: Folder, name: str) -> Folder | None:
    wanted = name.strip().lower()
    for child in parent.children:
        if (child.name or "").strip().lower() == wanted:
            return child
    return None


def _find_by_id(account: Account, folder_id: str) -> Folder | None:
    # Folder ids are long opaque strings; anything short is a name typo.
    if len(folder_id) < 40:
        return None
    for folder in walk_mail_folders(account):
        if folder.id == folder_id:
            return folder
    return None


def walk_mail_folders(account: Account):
    """Yield all mail folders (IPF.Note or untyped) under the IPM subtree."""
    for folder in account.msg_folder_root.walk():
        folder_class = folder.folder_class or ""
        if not folder_class or folder_class.startswith("IPF.Note"):
            yield folder


def get_message_by_id(message_id: str, require_message: bool = False):
    """Fetch a single mailbox item by its EWS item id.

    With require_message=True the item must be a regular email message -
    replying and forwarding are only defined for those. Other mailbox items
    (e.g. meeting invitations sitting in the inbox) can still be read,
    moved and deleted.
    """
    account = get_account()
    item = account.inbox.get(id=message_id)  # GetItem call, folder-independent
    if require_message and not isinstance(item, Message):
        raise EwsToolError(
            "This item is not a regular email message (probably a meeting "
            "invitation) - replying and forwarding are not supported for it."
        )
    return item
