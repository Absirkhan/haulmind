"""Delete the SQLite database and run folders (keeps the LLM cache).  ``make reset``"""

from __future__ import annotations

import shutil

from haulmind.config import get_settings


def main() -> int:
    s = get_settings()
    removed = []
    for p in (
        s.db_path,
        s.db_path.with_name(s.db_path.name + "-wal"),
        s.db_path.with_name(s.db_path.name + "-shm"),
    ):
        if p.exists():
            p.unlink()
            removed.append(str(p))
    if s.runs_dir.exists():
        shutil.rmtree(s.runs_dir, ignore_errors=True)
        removed.append(str(s.runs_dir))
    if s.quota_file.exists():
        s.quota_file.unlink()
        removed.append(str(s.quota_file))
    print("removed: " + (", ".join(removed) if removed else "nothing to remove"))
    print("run `uv run alembic upgrade head` (or `make setup`) to recreate the schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
