# Slim EWS MCP Server

A minimal local [MCP](https://modelcontextprotocol.io) server (stdio transport)
that gives Claude Desktop / Claude Code a controlled set of tools for working
with a mailbox on **Microsoft Exchange via EWS** — built on
[exchangelib](https://github.com/ecederstrand/exchangelib).

Designed for on-prem Exchange servers where IMAP is closed but
`https://…/EWS/Exchange.asmx` is reachable. Works with any EWS-enabled server;
every connection detail is driven by configuration, nothing is hardcoded.

**Scope:** mail only — list, read, reply, forward, move, soft-delete,
mark read/unread. No calendars, no contacts, no tasks. Safety and token
economy are prioritized over feature completeness.

## Safety model

- **Capability modes.** `EWS_MODE` bounds what the server can do at all:
  `full` (default), `draft` (reply/forward save to Drafts — the server cannot
  send anything), `read` (read-only; mutating tools are not even registered,
  so the model never sees them). See
  [Restricting what the server can do](#restricting-what-the-server-can-do).
- **Two-step confirmation, enforced in code.** `reply_email`, `forward_email`,
  `move_message` and `delete_message` do nothing when called with
  `confirm=false` (the default) — they return a preview (recipients, subject,
  exact text / what moves where). The actual send/move/delete code is
  unreachable without an explicit second call with `confirm=true`. This is an
  invariant, not a setting.
- **Soft delete only.** `delete_message` moves the message to Deleted Items;
  permanent deletion is not implemented at all.
- **Read-only by default elsewhere.** The only unconfirmed mutating action is
  `mark_read`.
- **Token-lean responses.** Listings return metadata + a ~200-char snippet,
  never bodies; bodies are converted HTML→Markdown with quoted history
  trimmed by default; attachments are listed by name/size and never
  downloaded.
- **No secrets in code or logs.** Credentials come only from environment
  variables / `.env`. Logs (stderr) contain operation metadata only — never
  bodies, subjects, passwords or auth headers.
- **TLS verification stays on** unless you explicitly opt out (see
  [TLS](#tls-and-internal-cas)).

## Tools

| Tool | What it does |
|---|---|
| `list_messages(folder, limit, unread_only, query)` | Newest-first metadata + snippet; `query` uses Exchange AQS syntax (`from:alice subject:report`) |
| `get_message(message_id, include_quoted)` | Full message as Markdown; quoted history trimmed unless `include_quoted=true` |
| `reply_email(message_id, body, reply_all, confirm)` | Reply / reply-all (two-step confirm) |
| `forward_email(message_id, to, body, confirm)` | Forward with original attachments (two-step confirm) |
| `move_message(message_id, target_folder, confirm)` | Move between folders (two-step confirm) |
| `delete_message(message_id, confirm)` | Soft delete to Deleted Items (two-step confirm) |
| `mark_read(message_id, read)` | Mark read/unread |
| `list_folders()` | Mail folder tree with ids and unread/total counts |

Folders are addressed by language-independent alias (`inbox`, `sent`,
`drafts`, `trash`, `junk`, `outbox`), by display-name path
(`Inbox/Projects/2026`), or by folder id from `list_folders`.

## Requirements

- Python **3.10+**
- Network access to the EWS endpoint of your Exchange server
- An account with password-based auth (Basic / NTLM / Digest / Kerberos).
  OAuth (Exchange Online) is not supported in this version.

## Installation

```bash
git clone https://github.com/Pal9yni4bi/slim-ews-mcp.git
cd slim-ews-mcp
python -m venv .venv

# Windows (Git Bash):        source .venv/Scripts/activate
# Windows (PowerShell):      .venv\Scripts\Activate.ps1
# Linux / macOS:             source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env   # then edit .env
```

## Configuration

Copy `.env.example` to `.env` and fill it in. `.env` is git-ignored; real
environment variables take precedence over the file.

| Variable | Required | Meaning |
|---|---|---|
| `EWS_ENDPOINT` | yes* | Full EWS URL, e.g. `https://mail.example.com/EWS/Exchange.asmx` |
| `EWS_AUTODISCOVER` | no | `true` = locate the endpoint via Autodiscover instead of `EWS_ENDPOINT` |
| `EWS_EMAIL` | yes | Primary SMTP address of the mailbox |
| `EWS_USERNAME` | no | Login if it differs from the email; for NTLM use `DOMAIN\user` |
| `EWS_PASSWORD` | yes | Password |
| `EWS_AUTH_TYPE` | no | `basic` / `ntlm` / `digest` / `gssapi` / `sspi`; empty = autodetect |
| `EWS_ACCESS_TYPE` | no | `delegate` (default) or `impersonation` |
| `EWS_MODE` | no | `full` (default), `draft` (never sends — saves to Drafts) or `read` (read-only) |
| `EWS_INSECURE_TLS` | no | **Dangerous.** `true` disables TLS verification — see below |
| `EWS_TIMEOUT` | no | HTTP timeout in seconds (default 30) |
| `EWS_MAX_LIST_LIMIT` | no | Hard cap for `list_messages` limit (default 100) |

\* not required when `EWS_AUTODISCOVER=true`.

### Restricting what the server can do

There are two independent layers, and they defend against different things.

**1. Client-side: `EWS_MODE`.** This bounds the server and the model driving
it — useful when you want Claude to draft replies for you but never to put
mail on the wire.

| Mode | Tools registered | Reply / forward with `confirm=true` |
|---|---|---|
| `full` (default) | all 8 | sends the message |
| `draft` | all 8 | saves to Drafts; **nothing is ever sent** |
| `read` | `list_messages`, `get_message`, `list_folders` | tool not registered |

In `draft` mode the send call is replaced by `save()` into the Drafts folder,
and the tool descriptions tell the model to report a saved draft rather than a
sent message. You review the draft in Outlook/OWA and press Send yourself.
`read` mode goes further: the mutating tools are never registered, so they do
not exist as far as the model is concerned (and a direct call is rejected
too).

**2. Server-side: Exchange permissions.** `EWS_MODE` is enforced by this
process. It does not restrict the *credentials* — anything holding them can
still send by other means. EWS has no per-operation ACL: a mailbox owner
authenticating as themselves can always send as themselves, and no EWS or
`Set-CASMailbox` setting changes that (`EWSEnabled` / `EWSAllowList` control
*which applications* may use EWS, not which operations they may perform).

To make "cannot send" a boundary the process cannot cross, connect as a
**separate service account** that has been granted access to the mailbox but
**not** the right to send as it. On on-prem Exchange:

```powershell
# read + create drafts in the target mailbox
Add-MailboxPermission -Identity user@example.com -User svc-claude `
  -AccessRights FullAccess -InheritanceType All

# deliberately NOT granted:
#   Add-ADPermission ... -ExtendedRights "Send As"
#   Set-Mailbox user@example.com -GrantSendOnBehalfTo svc-claude
```

Then point the server at the mailbox while authenticating as the service
account:

```
EWS_EMAIL=user@example.com
EWS_USERNAME=CORP\svc-claude
EWS_PASSWORD=...
EWS_ACCESS_TYPE=delegate
EWS_MODE=draft
```

`FullAccess` does not imply `SendAs` — reading and saving drafts work, while
any send attempt is refused by Exchange itself (`ErrorSendAsDenied` /
access denied), regardless of what the client asks for. For a stricter,
folder-level variant use `Add-MailboxFolderPermission` (e.g. `Reviewer` on
`:\Inbox`, `Editor` on `:\Drafts`) instead of `FullAccess`.

Note that `EWS_ACCESS_TYPE=delegate` by itself grants nothing and restricts
nothing — it only tells Exchange how to interpret the connection. The actual
permissions come from the cmdlets above.

### TLS and internal CAs

If your Exchange server uses a certificate issued by an internal corporate CA,
**do not disable verification** — point Python's `requests` at your CA bundle
instead:

1. Export the root CA certificate as a PEM/CRT file (from your IT department,
   or from the browser's certificate viewer on the OWA page).
2. Set the standard environment variable (in `.env` or system-wide):

   ```
   REQUESTS_CA_BUNDLE=C:\certs\corp-root-ca.pem
   ```

`EWS_INSECURE_TLS=true` exists as a **last resort only**. It disables
certificate verification entirely, which makes the connection vulnerable to
man-in-the-middle attacks — anyone on the network path can read your password
and your mail. The server logs a warning on every start while it is enabled.
Never use it outside a trusted network segment, and prefer fixing the CA
bundle instead.

## Hooking up to Claude

### Claude Desktop (classic versions)

Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "ews-mail": {
      "command": "C:\\path\\to\\slim-ews-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\slim-ews-mcp\\src\\main.py"],
      "env": { "PYTHONUTF8": "1" }
    }
  }
}
```

On Linux/macOS use `.venv/bin/python` as the command. Credentials are read
from `.env` in the project root, so they don't need to appear in the Claude
config. Restart Claude Desktop after editing.

### Claude Desktop (new unified app, 2025+)

The unified Claude Desktop ignores `mcpServers` in
`claude_desktop_config.json` (it strips the key on restart). Local stdio
servers are installed as **MCPB extension bundles** instead. A bundle is
just a zip containing one `manifest.json`:

```json
{
  "dxt_version": "0.2",
  "name": "ews-mail",
  "display_name": "EWS Mail",
  "version": "0.1.0",
  "description": "Safe EWS mail tools for Claude.",
  "author": { "name": "you" },
  "server": {
    "type": "binary",
    "entry_point": "run",
    "mcp_config": {
      "command": "C:\\path\\to\\slim-ews-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\slim-ews-mcp\\src\\main.py"],
      "env": { "PYTHONUTF8": "1" }
    }
  }
}
```

Zip the `manifest.json` (it must sit at the zip root), rename the archive to
`ews-mail.mcpb`, then in Claude Desktop open **Settings → Extensions** and
install the file (button or drag-and-drop). The absolute paths make the
bundle machine-specific — rebuild it per machine.

### Claude Code

Windows (Git Bash):

```bash
claude mcp add ews-mail -- /c/path/to/slim-ews-mcp/.venv/Scripts/python.exe /c/path/to/slim-ews-mcp/src/main.py
```

Linux / macOS:

```bash
claude mcp add ews-mail -- /path/to/slim-ews-mcp/.venv/bin/python /path/to/slim-ews-mcp/src/main.py
```

## Verifying the setup

1. **Config check:** from the project root run `.venv\Scripts\python.exe
   src\main.py` (Windows) or `.venv/bin/python src/main.py` (Linux/macOS) —
   the system `python` won't do, the dependencies live in the venv. With a
   bad/missing `.env` it exits immediately with a readable message; with a
   good one it starts silently and waits on stdin (Ctrl+C to stop).
2. **Reading works:** in Claude, ask *"list my 5 latest unread emails"* —
   you should get metadata and snippets from `list_messages`.
3. **Dry-run really blocks sending:** ask Claude to reply to some message.
   The first `reply_email` call must come back with `"status": "preview"`
   and nothing must appear in your Sent Items until you approve and Claude
   repeats the call with `confirm=true`.

## Troubleshooting

- **401 / authentication failed** — check `EWS_USERNAME` / `EWS_PASSWORD`;
  for NTLM the username usually needs the `DOMAIN\user` form (in `.env`,
  write the backslash as-is: `CORP\jdoe`). Try `EWS_AUTH_TYPE=ntlm`
  explicitly if autodetection picks the wrong scheme.
- **TLS certificate verification failed** — see
  [TLS and internal CAs](#tls-and-internal-cas).
- **"Message not found" after moving/deleting** — EWS item ids change when a
  message changes folders; re-run `list_messages` and use the fresh id.
- **Cannot reach the EWS endpoint** — verify the URL opens in a browser
  (it should ask for credentials or show a WSDL), check VPN/firewall.

## Project layout

```
src/
  main.py         entry point (stdio MCP server)
  config.py       env/.env loading and validation
  ews_client.py   exchangelib Account, folder resolution, error mapping
  tools.py        the 8 MCP tools + confirmation invariant
  html_to_md.py   HTML→Markdown conversion, quoted-history trimming
```

## License

MIT
