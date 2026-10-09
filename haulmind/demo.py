"""``make demo``: a small fleet, a fast clock and the scripted incidents, in one process.

Starts the web app with an in-process MCP server, streams about 20 simulated hours at 10
simulated minutes per real second, and opens the inbox. The HOS-risk incident lands roughly a
minute in; fuel and maintenance incidents arrive before it.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import threading
import time
import webbrowser

import uvicorn
from alembic import command
from alembic.config import Config

from haulmind.config import get_settings, reset_settings_cache


def _migrate() -> None:
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Haulmind demo")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--fleet-size", type=int, default=8)
    ap.add_argument("--hours", type=float, default=20)
    ap.add_argument("--speed", type=float, default=600, help="simulated seconds per real second")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)

    os.environ.update(
        {
            "SIM_SEED": str(args.seed),
            "SIM_FLEET_SIZE": str(args.fleet_size),
            "SIM_HOURS": str(args.hours),
            "SIM_SPEED": str(args.speed),
            "SIM_AUTOSTART": "true",
            "MCP_TRANSPORT": "inprocess",
        }
    )
    reset_settings_cache()
    s = get_settings()
    s.ensure_dirs()
    _migrate()
    from haulmind.rag.index import ensure_index

    ensure_index()
    port = args.port or s.web_port
    url = f"http://{s.web_host}:{port}/inbox"
    print(
        f"Haulmind demo: {args.fleet_size} trucks, {args.hours:g} simulated hours at {args.speed:g}x"
    )
    print(
        f"Provider: {s.effective_llm_provider}"
        + ("" if s.gemini_api_key else " (set GEMINI_API_KEY in .env for live Gemini)")
    )
    print(f"Open {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(2.5, lambda: webbrowser.open(url)).start()
    t0 = time.time()
    with contextlib.suppress(KeyboardInterrupt):
        uvicorn.run(
            "haulmind.web.app:create_app",
            factory=True,
            host=s.web_host,
            port=port,
            log_level="warning",
        )
    print(f"demo stopped after {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
