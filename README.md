# GroupTrip

Shared trip room: travelers add preferences, the app merges them into a group profile with conflict flags,
and returns three itineraries (Fastest / Max Experience / Cheapest) with scorecards. Feedback goes to memory
and the trip is re-planned. LLM = language, code = optimization, Mem0 = continuity.

## Run it

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn server:app --port 8000      # serves the UI and the API on one origin
```

Open **http://127.0.0.1:8000**. `.env` is optional: without `MEM0_API_KEY` memory is a local JSON file,
without `ANTHROPIC_API_KEY` explanations and feedback parsing fall back to templates/regex.

## Deploy (Vercel)

`vercel.json` routes every request to `api/index.py`, which serves the same FastAPI app, and `includeFiles`
bundles the root modules plus `destinations/` and `frontend/` (import tracing alone does not pick up the
JSON data or the HTML). Do not pin the Python runtime version - that fails the build with
`pin-version-mismatch`; Vercel picks the runtime from `requirements.txt`.

Set `MEM0_API_KEY` and `ANTHROPIC_API_KEY` as project env vars. The filesystem is read-only there, so
local-JSON memory falls back to `/tmp` (lost on cold start - use a real Mem0 key for a live demo) and
destination editing returns 503.

## Python API

```python
from data import list_destinations, load_destination, validate_traveler
from memory import TripMemory
from planner import plan, merge_profile
import explain

mem = TripMemory()                       # Mem0 if MEM0_API_KEY set, else local JSON (.local_memory.json)
GROUP = "group:yell24"                   # scoped to the room code; no shared/default group

# 1. join: pre-fill this traveler for THIS destination (None = new traveler, no memory)
got = mem.prefill("Nehmat", "la")        # {profile, sources{field:{destination,scope}}, customised_fields}
mem.save_profile("Nehmat", confirmed_form, trip_id="LATRIP", destination="la")

# 2. group profile + conflict flags (live, as people join / edit)
prof = merge_profile(travelers, load_destination("yellowstone"))   # prof["conflicts"] -> [{type, people, severity, message}]

# 3. plan (settings overrides any planner knob; adjustments override memory)
adj = mem.derive_adjustments(names, GROUP)        # memory -> planner inputs
result = plan(dest, travelers, days=3, adjustments={**adj, "max_walk_min": 90}, settings={"budget_rule": "median"})
#   result["itineraries"][mode] for mode in fastest | max_experience | cheapest
#   each: {label, headline, shared_with, days:[{day, stops:[{name,start,end,travel_min,cost,fit{person:0..1}}], ...}],
#          scorecard:{travel_min,dwell_min,total_cost,travel_share,satisfaction{person:{mean,min}},...}}
text = explain.explain_plans(result, mem.recall_for_plan(names, GROUP), adj)   # {overview, modes{}, memory_notes[]}

# 4. pick + feedback + re-plan (the pick's real satisfaction feeds the fairness ledger)
sat = {n: v["mean"] for n, v in result["itineraries"]["cheapest"]["scorecard"]["satisfaction"].items()}
mem.record_mode_choice(GROUP, "cheapest", trip_id, sat)
a = explain.parse_feedback("too much walking", dest)
mem.learn_feedback("Nehmat", "too much walking", trip_id, a)
result2 = plan(dest, travelers, 3, mem.derive_adjustments(names, GROUP))
explain.explain_replan(result["itineraries"]["cheapest"], result2["itineraries"]["cheapest"], "too much walking", a)
```

- `personas.json` is sample starter data; `python seed_memory.py --reset` loads an optional fictional history for dry runs. Neither is required - memory fills up from real use.
- New destination = drop a JSON in `destinations/` (see `yellowstone.json`). New persona = add to `personas.json`.
- `.env`: `ANTHROPIC_API_KEY`, `MEM0_API_KEY`. Both optional; without them the app falls back to local memory and templated explanations.
- Mem0 2.x needs identity inside `filters={"user_id": ...}`, not as a top-level kwarg. `memory.py` handles that.

## HTTP API (`server.py`)

Implements the contract in `frontend/README.md`. The UI is served from the same origin at `/`, so no CORS is needed in the browser (CORS is still open for a separately-served frontend).

| Endpoint | Notes |
|---|---|
| `POST /api/plan` | `room.destination` = key or label (`yellowstone`, `la`, `nyc`). Returns the contract plus extras: `conflicts[]`, `agent_notes[]`, `overview`, and per-plan `changes` (diff vs the previous plan for the same `room.code`, for the re-plan view). |
| `GET /api/memory/recall?user_id=Nehmat&destination=la` | `{name, fields, facts, sources, customised_fields, carried_from}` or `null`. Profiles are **per destination**: this destination's own saved form wins field by field, otherwise the most recent form from any other trip carries over. `sources[field].scope` is `customised_here`, `this_destination` or `carried` - badge accordingly ("from your Yellowstone trip" vs "you changed this for LA"). |
| `POST /api/memory/add` | `{name, text, meta:{type, ...}}`. `type`: `feedback` (parsed into constraints; pass `meta.destination`), `profile` (`meta.profile`), `compromise`, `mode_choice` (`meta.mode`), `group`. `meta.room` scopes group memory. |
| `GET /api/destinations`, `GET /api/personas` | Dropdown data and the sample personas in `personas.json` (starter data only - nothing depends on them). |
| `GET /api/settings` | Every planner knob with its default. |
| `GET/PUT /api/destinations/{name}`, `PUT/DELETE .../places/{id}`, `PUT .../lodgings/{id}`, `PATCH .../travel` | Live-edit destination data; writes `destinations/*.json` and is picked up by the next plan. Invalid data returns 422 with the reason. |
| `POST /api/reset` | Wipe local memory and cached plans (refuses on a live Mem0 project). |
| `POST /api/seed` | Optional fictional sample history for dry runs. Never loaded automatically. |

### Nothing is staged
Group memory is scoped to `room.code` only - a room knows only what it has actually done, and group-level writes without `meta.room` are rejected. Fairness is computed, not written: `record_mode_choice` stores the chosen plan's real per-person satisfaction, and whoever the picked plans keep serving worst accumulates a deficit (`fairness` in the plan response) and gets priority. Explicit "X compromised" notes add to the same ledger.

### Memory is always overridable
`POST /api/plan` takes `settings` (any knob from `GET /api/settings`), `adjustments` (override a memory-derived constraint; `null` clears one), `ignore_memory: true` (plan from the submitted forms alone) and `remember: false` (plan without saving the forms). The response echoes `adjustments_from_memory`, `adjustments` (what was used) and `adjustments_overridden`, so the UI can always show what memory wanted and what the user chose instead. Submitted form values always beat memory.

Vocabulary translation (see `server.py`): pace balanced/packed to moderate/active; views/relax to scenic/coffee; vegan to vegetarian; `diet` list to one value; satisfaction 0-1 to 0-100; plan keys `max_experience` to `max`.
Group memory is `group:<room.code>`; there is no fallback group.

## How the three plans are chosen (`planner.plan`)
For each request the planner builds a pool of ~50-80 distinct greedy itineraries (16 weight sets x 5 place-subset variants), drops any that fail shared floors (2+ activities/day, worst-off traveler near the best, no day over 5h of travel), then picks per mode by its headline metric: **Fastest** = lowest share of the trip spent travelling, **Cheapest** = lowest cost, **Max Experience** = most time at spots. Each plan carries `headline` and `shared_with` (set when one itinerary genuinely wins two modes and the runner-up would be >15% worse).
Satisfaction per traveler: a neutral stop scores 0.45 and two matching interests 1.0, then dealbreaker/budget penalties; reported 0-100.

Nothing above is hardcoded policy: `planner.DEFAULT_SETTINGS` holds all 22 knobs (wake buffer, budget rule min/median/none, pace caps, least-misery weight, what "long walks"/"early mornings"/"late nights"/"expensive meals" mean, dealbreaker penalty vs hard exclude, meal windows, day window, selection floors). Pass overrides per request; unknown keys and bad values raise 422.
