"""Evaluation harness.

    uv run python -m evals.run_eval --all                            # base + learning loop + ablations
    uv run python -m evals.run_eval --all --llm-cache-mode replay    # zero network calls
    uv run python -m evals.run_eval --base --quick                   # small fleet, fast

Writes evals/results.md and evals/results.json, and refreshes the results block in README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import haulmind.mcp_server.server as srv
from evals.manager_sim import decide_all
from evals.metrics import Metrics, score
from haulmind.config import Settings, get_settings, reset_settings_cache
from haulmind.db.engine import get_engine
from haulmind.events.bus import EventBus
from haulmind.llm.base import CacheMiss, QuotaExhausted
from haulmind.llm.factory import build_llm_client
from haulmind.logging_setup import configure_logging, get_logger
from haulmind.mcp_server.client import ToolClient
from haulmind.mcp_server.server import build_server
from haulmind.orchestrator import Orchestrator, replay_events
from haulmind.simulator import SimConfig, generate, install_run
from haulmind.simulator.scenarios import ScenarioMix
from haulmind.util import to_naive_utc

log = get_logger(__name__)
HERE = Path(__file__).parent
README = HERE.parent / "README.md"
START = "<!-- EVAL_RESULTS_START -->"
END = "<!-- EVAL_RESULTS_END -->"


@dataclass
class Scenario:
    name: str
    seed: int = 42
    fleet_size: int = 25
    hours: float = 48.0
    mix: ScenarioMix | None = None
    overrides: dict[str, Any] = field(default_factory=dict)

    def config(self) -> SimConfig:
        return SimConfig(
            seed=self.seed,
            fleet_size=self.fleet_size,
            hours=self.hours,
            mix=self.mix,
            label=self.name,
        )


def _settings(base: Settings, **overrides: Any) -> Settings:
    data = base.model_dump()
    data.update(overrides)
    return Settings(**data)


async def replay(
    scn: Scenario,
    settings: Settings,
    *,
    label: str,
    cuts_hours: list[float] | None = None,
    manager: bool = False,
) -> tuple[Metrics, dict[str, Any]]:
    """Install a fresh run and replay it through the agent pipeline. Returns metrics + extras.

    ``cuts_hours`` splits the replay into segments (simulated days). After each segment the
    scripted manager, if enabled, decides everything proposed so far, which feeds agent memory
    before the next segment streams.
    """
    sim = generate(scn.config())
    install_run(sim)
    llm = build_llm_client(settings)
    srv._server = build_server()
    extras: dict[str, Any] = {"run_id": sim.run_id, "gt": sim.ground_truth, "manager": []}
    t0 = time.time()
    async with ToolClient(settings, caller="eval-agent") as tc:
        orch = Orchestrator(sim.run_id, tools=tc, llm=llm, settings=settings)
        try:
            bounds = [
                to_naive_utc(sim.config.start + timedelta(hours=h)) for h in (cuts_hours or [])
            ]
            edges = [
                to_naive_utc(sim.config.start),
                *bounds,
                to_naive_utc(sim.config.end) + timedelta(days=1),
            ]
            bus = EventBus()
            for lo, hi in pairwise(edges):
                segment = [e for e in sim.events if lo <= to_naive_utc(e.ts) < hi]
                if segment:
                    await replay_events(orch, segment, bus, start_seq=segment[0].seq)
                if manager and hi < edges[-1]:
                    st = await decide_all(sim.run_id, sim.ground_truth, tc, until_sim_ts=hi)
                    extras["manager"].append(st.__dict__)
            extras["edges"] = edges
        except QuotaExhausted as e:
            extras["stopped"] = f"quota exhausted: {e}"
            log.warning("eval stopped by quota guard", error=str(e))
        except CacheMiss as e:
            raise SystemExit(f"replay mode needs a warm cache: {e}") from e
        wall = time.time() - t0
        m = score(
            sim.run_id,
            sim.ground_truth,
            label=label,
            provider=llm.provider,
            wall_seconds=round(wall, 1),
            tool_calls=tc.calls,
        )
        extras["orch_stats"] = orch.stats.as_dict()
        extras["tool_calls"] = tc.calls
    return m, extras


def _row(m: Metrics) -> str:
    return (
        f"| {m.label} | {m.recall:.0%} ({m.detected}/{m.gt_positives}) | {m.false_alert_rate:.0%} ({m.false_alerts}/{m.alerts}) | "
        f"{m.action_correctness:.0%} | {m.policy_violations} | {m.latency_p50_ms:.0f} / {m.latency_p95_ms:.0f} ms | "
        f"{m.llm_requests_per_incident:.1f} / {m.tokens_per_incident:.0f} | {m.filtered_pct:.1f}% |"
    )


HEADER = (
    "| Run | Detection recall | False-alert rate | Action correctness | Policy violations | Latency p50 / p95 | LLM requests / tokens per incident | Events filtered before any LLM |\n"
    "|---|---|---|---|---|---|---|---|"
)


def render_markdown(results: dict[str, Any], settings: Settings, cache_mode: str) -> str:
    base: Metrics = results["base"]
    scn: Scenario = results["scenario"]
    provider_note = (
        f" (triage `{settings.llm_model_triage}`, planner `{settings.llm_model_planner}`)"
        if base.provider == "gemini"
        else " (deterministic offline heuristics; set GEMINI_API_KEY to evaluate the real model)"
    )
    lines = [
        "## Evaluation results",
        "",
        f"Generated by `make eval` on {time.strftime('%Y-%m-%d %H:%M')} ({platform.system()}, Python {platform.python_version()}). "
        f"LLM provider: **{base.provider}**{provider_note}. Cache mode: `{cache_mode}`.",
        "",
        f"Base scenario: seed {scn.seed}, {scn.fleet_size} trucks, {scn.hours:g} simulated hours, "
        f"{base.events_total} events, {base.gt_positives} injected incidents and {base.gt_benign} benign look-alikes. "
        f"Wall time {base.wall_seconds:.0f}s, {base.tool_calls} MCP tool calls.",
        "",
        "### Core metrics",
        "",
        HEADER,
        _row(base),
        "",
        "Per scenario type (base run):",
        "",
        "| Type | Injected | Detected | Expected actions proposed |",
        "|---|---|---|---|",
    ]
    for t, c in sorted(base.per_type.items()):
        lines.append(f"| {t} | {c['total']} | {c['detected']} | {c['correct']} |")
    if base.missed:
        lines += ["", "Missed: " + "; ".join(base.missed) + "."]
    if base.false_alert_ids:
        lines += ["", "False alerts: " + "; ".join(base.false_alert_ids) + "."]

    if "learning" in results:
        lrn = results["learning"]
        lscn: Scenario = lrn["scenario"]
        lmix = lscn.mix or ScenarioMix()
        lines += [
            "",
            "### Learning loop: precision before vs after manager decisions",
            "",
            f"Scenario: {lscn.fleet_size} trucks, {lscn.hours:g} simulated hours with extra benign look-alikes "
            f"({lmix.hos_near_miss} HOS near misses, {lmix.nuisance_dtc} nuisance-code trucks, {lmix.reefer_fuel_lookalike} reefer unit). "
            "At the end of each simulated day a scripted manager reviews the inbox: approves actions on real incidents, rejects look-alikes with a reason code. "
            "With memory on, those decisions tune per-driver/per-vehicle thresholds before the next day streams; with memory off they are recorded but ignored.",
            "",
            "| Day | Memory | Alerts | True | False | Precision | Recall |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in lrn["rows"]:
            lines.append(
                f"| {row['phase']} | {row['memory']} | {row['alerts']} | {row['true']} | {row['false']} | {row['precision']:.0%} | {row['recall']:.0%} |"
            )
        lines += ["", f"Manager decisions simulated per arm: {lrn['decisions']}."]

    if "ablations" in results:
        lines += ["", "### Ablations", "", HEADER, _row(base)]
        for m in results["ablations"]:
            lines.append(_row(m))
        if results.get("ablation_notes"):
            lines += ["", *[f"- {n}" for n in results["ablation_notes"]]]
    lines += [
        "",
        "Definitions: an *alert* is an incident with at least one action proposed to a manager or executed automatically; "
        "*false-alert rate* is the share of alerts that match no injected incident; *action correctness* is the share of detected "
        "incidents whose proposed actions include every expected action; *policy violations* counts tier-2/3 side effects without a "
        "bound approval (must be 0). Latency is wall time per incident trace. Tokens are prompt plus output per incident.",
    ]
    return "\n".join(lines) + "\n"


def splice_readme(block: str) -> bool:
    if not README.exists():
        return False
    text = README.read_text(encoding="utf-8")
    if START not in text or END not in text:
        return False
    new = re.sub(
        rf"{re.escape(START)}.*?{re.escape(END)}",
        lambda _m: f"{START}\n{block}\n{END}",
        text,
        flags=re.DOTALL,
    )
    README.write_text(new, encoding="utf-8", newline="\n")
    return True


def _phase_rows(
    run_id: str, gt: Any, edges: list[datetime], provider: str, memory: str
) -> list[dict[str, Any]]:
    rows = []
    for day, (lo, hi) in enumerate(pairwise(edges), start=1):
        mm = score(run_id, gt, label=f"day {day}", provider=provider, phase_filter=(lo, hi))
        rows.append(
            {
                "phase": f"day {day}" + (" (no decisions yet)" if day == 1 else ""),
                "memory": memory,
                "alerts": mm.alerts,
                "true": mm.true_alerts,
                "false": mm.false_alerts,
                "precision": mm.precision,
                "recall": mm.recall,
            }
        )
    return rows


async def main_async(args: argparse.Namespace) -> int:
    reset_settings_cache()
    base_settings = get_settings()
    settings = _settings(
        base_settings,
        llm_cache_mode=args.llm_cache_mode,
        memory_enabled=True,
        agent_mode="multi",
        rag_enabled=True,
    )
    configure_logging("WARNING")
    get_engine()
    scn = Scenario(
        "base",
        seed=args.seed,
        fleet_size=12 if args.quick else args.fleet_size,
        hours=24 if args.quick else args.hours,
    )
    results: dict[str, Any] = {"scenario": scn}
    do_base = args.all or args.base or not (args.learning or args.ablations)

    if do_base:
        print(
            f"[base] seed={scn.seed} fleet={scn.fleet_size} hours={scn.hours:g} provider={settings.effective_llm_provider}"
        )
        m, _ = await replay(scn, settings, label="multi-agent, RAG, memory (base)")
        results["base"] = m
        print(
            f"  recall={m.recall:.0%} false_alert_rate={m.false_alert_rate:.0%} correctness={m.action_correctness:.0%} "
            f"violations={m.policy_violations} filtered={m.filtered_pct:.1f}% llm/incident={m.llm_requests_per_incident}"
        )
        if m.missed:
            print("  missed: " + "; ".join(m.missed))
        if m.false_alert_ids:
            print("  false alerts: " + "; ".join(m.false_alert_ids))

    if args.all or args.learning:
        mix = ScenarioMix(
            hos_violation_risk=3,
            failing_component=2,
            fuel_fraud=2,
            harsh_driving_pattern=1,
            hos_near_miss=5,
            nuisance_dtc=2,
            reefer_fuel_lookalike=1,
        )
        lscn = Scenario(
            "learning",
            seed=args.seed,
            fleet_size=12 if args.quick else args.fleet_size,
            hours=72,
            mix=mix,
        )
        cuts = [24.0, 48.0]
        rows: list[dict[str, Any]] = []
        decisions = 0
        for memory in (True, False):
            s2 = _settings(settings, memory_enabled=memory)
            label = "on" if memory else "off"
            print(f"[learning] memory={label} fleet={lscn.fleet_size} hours={lscn.hours:g}")
            m_all, extras = await replay(
                lscn, s2, label=f"learning memory={label}", cuts_hours=cuts, manager=True
            )
            rows += _phase_rows(
                extras["run_id"], extras["gt"], extras["edges"], m_all.provider, label
            )
            for r in rows[-len(cuts) - 1 :]:
                print(
                    f"  {r['phase']}: alerts={r['alerts']} true={r['true']} false={r['false']} precision={r['precision']:.0%} recall={r['recall']:.0%}"
                )
            if memory:
                decisions = sum(sum(st.values()) for st in extras["manager"])
        results["learning"] = {"scenario": lscn, "cuts": cuts, "rows": rows, "decisions": decisions}

    if args.all or args.ablations:
        abl: list[Metrics] = []
        notes: list[str] = []
        for label, over in (
            ("single agent (one LLM call), RAG, memory", {"agent_mode": "single"}),
            ("multi-agent, no RAG, memory", {"rag_enabled": False}),
        ):
            print(f"[ablation] {label}")
            m, _ = await replay(scn, _settings(settings, **over), label=label)
            abl.append(m)
            print(
                f"  recall={m.recall:.0%} false_alert_rate={m.false_alert_rate:.0%} correctness={m.action_correctness:.0%} "
                f"violations={m.policy_violations} llm/incident={m.llm_requests_per_incident}"
            )
        if settings.effective_llm_provider == "gemini":
            lite = _settings(settings, llm_model_planner=settings.llm_model_triage)
            print(
                f"[ablation] planner = {lite.llm_model_planner} instead of {settings.llm_model_planner}"
            )
            m, _ = await replay(scn, lite, label=f"multi-agent, planner={lite.llm_model_planner}")
            abl.append(m)
            print(
                f"  recall={m.recall:.0%} false_alert_rate={m.false_alert_rate:.0%} correctness={m.action_correctness:.0%} p50={m.latency_p50_ms:.0f}ms"
            )
        else:
            notes.append(
                "Flash-Lite vs Flash planner ablation skipped: no GEMINI_API_KEY, both roles ran on the offline stub."
            )
        results["ablations"] = abl
        results["ablation_notes"] = notes

    if "base" in results:
        md = render_markdown(results, settings, args.llm_cache_mode)
        (HERE / "results.md").write_text(md, encoding="utf-8", newline="\n")
        payload: dict[str, Any] = {
            "scenario": results["scenario"].__dict__,
            "base": results["base"].as_dict(),
        }
        if "ablations" in results:
            payload["ablations"] = [m.as_dict() for m in results["ablations"]]
            payload["ablation_notes"] = results.get("ablation_notes", [])
        if "learning" in results:
            lrn = results["learning"]
            payload["learning"] = {
                "scenario": {
                    **lrn["scenario"].__dict__,
                    "mix": lrn["scenario"].mix.model_dump() if lrn["scenario"].mix else None,
                },
                "cuts": lrn["cuts"],
                "rows": lrn["rows"],
                "decisions": lrn["decisions"],
            }
        (HERE / "results.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8", newline="\n"
        )
        spliced = splice_readme(md)
        print(
            "\nwrote evals/results.md and evals/results.json"
            + (" and refreshed README results block" if spliced else "")
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Haulmind evaluation harness")
    ap.add_argument("--all", action="store_true", help="base + learning loop + ablations")
    ap.add_argument("--base", action="store_true")
    ap.add_argument("--learning", action="store_true")
    ap.add_argument("--ablations", action="store_true")
    ap.add_argument("--quick", action="store_true", help="12 trucks / 24h instead of 25 / 48h")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--fleet-size", type=int, default=25)
    ap.add_argument("--hours", type=float, default=48.0)
    ap.add_argument("--llm-cache-mode", choices=["live", "replay", "off"], default="live")
    args = ap.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
