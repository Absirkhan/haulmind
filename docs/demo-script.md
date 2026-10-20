# Haulmind demo script (about 2 minutes)

One incident, start to finish: a driver who is about to run out of legal drive time, caught
by the agent, investigated with fleet tools, and resolved with one click.

## Before recording

```bash
cp .env.example .env        # add GEMINI_API_KEY or LLM_API_KEY for a live model; without one the offline stub runs
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

Point at the top bar: the fleet clock on its 24-hour log strip, ticking as the simulated
day runs. The share of readings handled by plain rules lives on the
"How well it works" page (it stays above 95%).

**0:20 - Watch it filter (Problems found page)**
"Thousands of GPS pings and engine readings go by. Rules drop almost all of them. Only a
handful of clusters ever reach a language model, and each one becomes an incident with a
cost: model calls, tokens, latency."

**0:40 - The incident lands (Inbox, Needs your OK tab)**
Click the item. Read the headline, which is built from the facts rather than written by the
model: "Frankie Castillo will run out of drive time about an hour before reaching Houston."

Walk the panel top to bottom:
- *Why it matters*, how sure Haulmind is, and the drive-time strip.
- *What Haulmind will do*: the step in plain words, with the trip, the drivers and the reason.
- *What Haulmind checked*: each step as a sentence ("Looked at the driver's remaining hours")
  with what it found. Raw tool names and payloads sit under "Technical details", collapsed.

**1:05 - Autonomy tiers (switch to Handled for you)**
"The same incident already produced a low-risk action: a message telling the driver to plan a
safe stop. That ran on its own, rate-limited and checked against a block list. The handoff
costs money and touches a customer, so it waits for you. Grounding a truck or suspending a
fuel card would only ever be a recommendation."

**1:25 - Approve**
Back on the pending item, press `a` or click "Approve the handoff". A toast offers Undo for a
few seconds, then the approval is sent, bound to those exact arguments; the gateway validates
it and executes the reassignment. Open History for a second: found a problem, asked for your
OK, you approved, trip handed over, all attributed.

**1:45 - Learning and tracing (optional close)**
Dismiss a weaker item with "This driver manages their own hours". The notice says what
Haulmind will remember. Press `?` to show the keyboard shortcuts, or `Ctrl K` to jump to a
truck or driver by name. The technical trace is one disclosure away if anyone asks.

**2:00 - Close**
"Everything runs on a laptop with SQLite and a free Gemini key. The policy that decides what
runs automatically is code, not a prompt, and the tests prove an agent cannot cross a tier."

## Variations

- Live model: set `GEMINI_API_KEY` (or `LLM_API_KEY` for Groq and other OpenAI-compatible
  providers) and `make demo`; the trace page shows real model names and
  token counts. At 5 requests per minute the first incident takes about a minute longer.
- Faster or slower clock: change the speed selector in the status bar at any time.
- Claude Desktop: run `uv run python -m haulmind.mcp_server --transport stdio` from a Claude
  Desktop MCP config and ask it to check driver D-07's hours; the same tiered gateway applies.
