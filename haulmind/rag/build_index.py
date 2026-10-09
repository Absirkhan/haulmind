"""CLI: build (or rebuild) the FAISS index.  ``uv run python -m haulmind.rag.build_index``"""

from __future__ import annotations

import argparse

from haulmind.config import get_settings
from haulmind.logging_setup import configure_logging
from haulmind.rag.index import build_index


def main(argv: list[str] | None = None) -> int:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--query", help="run a test query after building")
    args = ap.parse_args(argv)
    s.ensure_dirs()
    idx = build_index(force=args.force)
    print(f"index ready: {len(idx.chunks)} chunks from {len({c.doc for c in idx.chunks})} docs")
    if args.query:
        for hit in idx.search(args.query, k=3):
            print(f"  {hit.score:.3f}  {hit.citation}  — {hit.chunk.title} / {hit.chunk.section}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
