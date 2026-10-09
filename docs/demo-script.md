# Haulmind demo script (about 2 minutes)

One incident, start to finish: a driver who is about to run out of legal drive time, caught
by the agent, investigated with fleet tools, and resolved with one click.

## Before recording

```bash
cp .env.example .env        # add GEMINI_API_KEY for live Gemini; without it the offline stub runs
make setup
make demo                   # 8 trucks, 20 simulated hours at 10 sim-minutes per second
```

`make demo` opens `http://127.0.0.1:8000/inbox`. The HOS incident on driver D-07 appears about
65 seconds in; a fuel-card anomaly and a cooling fault arrive before it. If you want the
inbox populated before you start talking, let it run for 90 seconds, then pause the stream.

## Script

**0:00 - The problem (inbox, empty or nearly empty)**
"Fleet managers drown in alerts: HOS clocks, harsh braking, fault codes, fuel anomalies.
Haulmind watches the same telematics stream, but instead of alerting it investigates and hands
you a short queue of actions you can approve in one click."

Point at the status bar: the simulated clock, events per second, and the percentage of events
handled by rules before any model call (it stays above 95%).

**0:20 - Watch it filter (Incidents page)**
"Thousands of GPS pings and engine readings go by. Rules drop almost all of them. Only a
handful of clusters ever reach a language model, and each one becomes an incident with a
cost: model calls, tokens, latency."

**0:40 - The incident lands (Inbox, Needs approval tab)**
Click "Hand off trip to spare driver". Read the summary:
"Driver D-07 will run out of drive time about an hour before reaching the destination."

Walk the panel top to bottom:
- *Why the agent proposes this*: the planner's reasoning and the verifier's check.
- *What will run*: the exact tool and arguments, nothing hidden.
- *Evidence*: the HOS clock, the vehicle status, the rested spare driver found by
  `find_idle_vehicles`, each labelled E1, E2, E3.

**1:05 - Autonomy tiers (switch to Ran automatically)**
"The same incident already produced a low-risk action: a message telling the driver to plan a
safe stop. That ran on its own, rate-limited and checked against a block list. The handoff
costs money and touches a customer, so it waits for you. Grounding a truck or suspending a
fuel card would only ever be a recommendation."

**1:25 - Approve**
Back on the pending item, click "Approve and run". The approval is bound to those exact
arguments; the MCP gateway validates it, executes the reassignment, and the status flips to
executed. Open the audit log for a second: incident opened, actions proposed, approval,
execution, all attributed.

**1:45 - Learning and tracing (optional close)**
Reject a weaker item with "Driver manages the clock". The notice shows the memory update:
the alert threshold for that driver tightens, and the fleet-wide default tightens after a
couple of such rejections. Then open the trace link: every agent step, tool call and model
call with latency and token counts.

**2:00 - Close**
"Everything runs on a laptop with SQLite and a free Gemini key. The policy that decides what
runs automatically is code, not a prompt, and the tests prove an agent cannot cross a tier."

## Variations

- Live Gemini: set `GEMINI_API_KEY` and `make demo`; the trace page shows real model names and
  token counts. At 5 requests per minute the first incident takes about a minute longer.
- Faster or slower clock: change the speed selector in the status bar at any time.
- Claude Desktop: run `uv run python -m haulmind.mcp_server --transport stdio` from a Claude
  Desktop MCP config and ask it to check driver D-07's hours; the same tiered gateway applies.
