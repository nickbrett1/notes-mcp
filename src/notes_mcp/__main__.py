"""Entry point: ``python -m notes_mcp [--transport ...]``.

Default transport is stdio (a local client spawns the process). For mcphub the
container runs streamable-http on ``/mcp`` with stateless requests, so no
session is pinned across POSTs.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Sequence
from typing import Any

from mcp.server import MCPServer

from .server import mcp

STREAMABLE_HTTP_PATH = "/mcp"
DEFAULT_PORT = 8767


def _run(server: MCPServer, transport: str, **kwargs: Any) -> None:
    server.run(transport, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notes_mcp",
        description="Obsidian vault MCP server (stdio by default).",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport to serve on (default: stdio).",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address for streamable-http (default: 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("NOTES_PORT", DEFAULT_PORT)),
        help=f"Port for streamable-http (default: {DEFAULT_PORT}).",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Callable[..., None] = _run,
) -> None:
    """Parse arguments and serve. ``run`` is injectable for tests."""
    args = build_parser().parse_args(argv)
    if args.transport == "streamable-http":
        run(
            mcp,
            "streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path=STREAMABLE_HTTP_PATH,
            stateless_http=True,
        )
    else:
        run(mcp, "stdio")


if __name__ == "__main__":
    main()
