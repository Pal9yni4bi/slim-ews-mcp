"""Entry point: local MCP server (stdio) exposing an EWS mailbox to Claude.

Run with: python src/main.py
Configuration comes from environment variables / .env (see .env.example).
"""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

import tools
from config import ConfigError, load_config


def main() -> None:
    # stdout is reserved for the MCP protocol - all logging goes to stderr.
    # Only operation metadata is ever logged (see tools.py), never message
    # bodies, subjects or credentials.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.getLogger("exchangelib").setLevel(logging.WARNING)

    if sys.platform == "win32":
        # MCP stdio speaks UTF-8; Windows consoles may default to a legacy
        # codepage, which would corrupt non-ASCII subjects and bodies.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass

    try:
        load_config()  # fail fast with a readable message on bad config
    except ConfigError as exc:
        print(f"ews-mcp: {exc}", file=sys.stderr)
        sys.exit(1)

    mcp = FastMCP("ews-mail")
    tools.register(mcp)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
