"""CLI: generate a seeded run, install it, and (optionally) persist every event for replay.

uv run python -m haulmind.simulator --seed 42 --hours 48 --fleet-size 25
"""

from __future__ import annotations

import argparse
import collections

from haulmind.config import get_settings
from haulmind.db.engine import get_engine
from haulmind.logging_setup import configure_logging
from haulmind.simulator.generator import SimConfig, generate
from haulmind.simulator.runner import install_run


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    ap = argparse.ArgumentParser(description="Haulmind simulator")
    ap.add_argument("--seed", type=int, default=settings.sim_seed)
    ap.add_argument("--fleet-size", type=int, default=settings.sim_fleet_size)
    ap.add_argument("--hours", type=float, default=settings.sim_hours)
    ap.add_argument("--label", default="")
    ap.add_argument("--no-install", action="store_true", help="only print a summary")
    ap.add_argument("--persist-events", action="store_true", help="write all events now")
    args = ap.parse_args(argv)

    cfg = SimConfig(seed=args.seed, fleet_size=args.fleet_size, hours=args.hours, label=args.label)
    sim = generate(cfg)
    counts = collections.Counter(e.type.value for e in sim.events)
    print(f"run_id: {sim.run_id}")
    print(
        f"events: {len(sim.events)}  ("
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        + ")"
    )
    gt = sim.ground_truth
    print(f"ground truth: {len(gt.positives)} positives, {len(gt.benigns)} benign lookalikes")
    for g in sim.ground_truth.incidents:
        flag = "benign " if g.benign else "        "
        window = f"{g.start_ts:%a %H:%M} -> {g.end_ts:%a %H:%M}"
        print(f"  {flag}{g.id:<16} {g.type:<24} {g.vehicle_id} {g.driver_id} {window}")
    if not args.no_install:
        get_engine()
        install_run(sim, persist_events=args.persist_events)
        print("installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
