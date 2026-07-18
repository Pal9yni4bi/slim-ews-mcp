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
| `EWS_INSECURE_TLS` | no | **Dangerous.** `true` disables TLS verification — see below |
| `EWS_TIMEOUT` | no | HTTP timeout in seconds (default 30) |
| `EWS_MAX_LIST_LIMIT` | no | Hard cap for `list_messages` limit (default 100) |

\* not required when `EWS_AUTODISCOVER=true`.

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

### Claude Desktop

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

1. **Config check:** run `python src/main.py` from the project root. With a
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
