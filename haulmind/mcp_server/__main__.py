"""Run the MCP server.

uv run python -m haulmind.mcp_server --transport http     # for make run
uv run python -m haulmind.mcp_server --transport stdio    # for Claude Desktop
"""

from __future__ import annotations

import argparse
import sys

from haulmind.config import get_settings
from haulmind.db.engine import get_engine
from haulmind.logging_setup import configure_logging, get_logger
from haulmind.mcp_server.server import build_server


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description="Haulmind MCP server")
    ap.add_argument("--transport", choices=["http", "stdio"], default="http")
    ap.add_argument("--host", default=settings.mcp_host)
    ap.add_argument("--port", type=int, default=settings.mcp_port)
    args = ap.parse_args(argv)

    # stdio transport owns stdout; all logging goes to stderr (structlog default here).
    configure_logging(
        settings.log_level if args.transport == "http" else "WARNING", settings.log_json
    )
    log = get_logger(__name__)
    get_engine()
    from haulmind.rag.index import ensure_index

    ensure_index()
    server = build_server()
    if args.transport == "stdio":
        server.run(transport="stdio")
        return 0
    log.info("mcp server listening", url=f"http://{args.host}:{args.port}/mcp")
    print(f"MCP server: http://{args.host}:{args.port}/mcp", file=sys.stderr, flush=True)
    server.run(transport="streamable-http", host=args.host, port=args.port, json_response=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
