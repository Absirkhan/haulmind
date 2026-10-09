"""Run the web process (UI + event bus + simulator + agents).

uv run python -m haulmind.web                       # in-process MCP, single process
uv run python -m haulmind.web --mcp-transport http  # talk to the separate MCP server
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from haulmind.config import get_settings, reset_settings_cache


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Haulmind web process")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--mcp-transport", choices=["http", "inprocess"], default=None)
    ap.add_argument(
        "--wait-for-mcp", action="store_true", help="retry MCP connection for up to 60s"
    )
    ap.add_argument("--no-autostart", action="store_true", help="start with the simulator paused")
    ap.add_argument(
        "--no-runtime", action="store_true", help="UI only; do not start simulator/agents"
    )
    args = ap.parse_args(argv)
    if args.mcp_transport:
        os.environ["MCP_TRANSPORT"] = args.mcp_transport
    if args.no_autostart:
        os.environ["SIM_AUTOSTART"] = "false"
    if args.wait_for_mcp:
        os.environ["HAULMIND_WAIT_FOR_MCP"] = "1"
    if args.no_runtime:
        os.environ["HAULMIND_NO_RUNTIME"] = "1"
    reset_settings_cache()
    s = get_settings()
    uvicorn.run(
        "haulmind.web.app:create_app",
        factory=True,
        host=args.host or s.web_host,
        port=args.port or s.web_port,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
