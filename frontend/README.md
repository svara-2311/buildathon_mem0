# GroupTrip: front end

Shared trip room where each traveler adds preferences, the app merges them into a group
profile (with conflict flags), and an agent returns three plans (Fastest / Max Experience /
Cheapest) with scorecards. Feedback is written to memory and the trip is re-planned.

Single static file, no build step.

The backend serves this file, so just run the server from the repo root and open http://127.0.0.1:8000:

```bash
./venv/bin/uvicorn server:app --port 8000
```

(Serving it standalone with `python3 -m http.server 8000` also works; it then calls the API on
`http://127.0.0.1:8000` cross-origin.)

**Wired up:** `planTrip`, `mem0Recall`, `mem0Add`, `mem0SaveProfile` and `mem0AddGroup` now call the real
API. The destination dropdown is populated from `GET /api/destinations`, and lodgings from the chosen
destination. The mock optimizer, the seeded SF places and the fake Priya/Sam memory are gone.

## Where the backend plugs in

Everything the UI needs from the backend goes through **three functions** near the top of the
`<script>` in `index.html`. Each has the real `fetch` call sketched in a comment right above the mock.

| Function | Replace with | Notes |
|---|---|---|
| `planTrip(payload)` | `POST /api/plan` | Optimizer + LLM explanation. Contract below. |
| `mem0Recall(name)` | `GET /api/memory/recall?user_id=` | Returns remembered profile fields + facts, or `null`. Drives the "from past trips" badges. |
| `mem0Add(name, text, meta)` / `mem0SaveProfile(m)` / `mem0AddGroup(code, text)` | `POST /api/memory/add` | Feedback, fairness ("compromised") and group-pick writes. |

The mock optimizer and the seeded SF places have been deleted; all trip data now comes from the backend.

## `POST /api/plan`

Request:

```json
{
  "room": { "code": "AB12C", "destination": "yellowstone", "days": 2, "start_date": null, "lodging": "Canyon Lodge & Cabins" },
  "members": [
    {
      "name": "Nehmat",
      "wake_time": "09:30",
      "rest_windows": ["14:00-15:00"],
      "diet": ["vegetarian"],
      "budget_per_day": 120,
      "pace": "relaxed",
      "interests": ["museums", "coffee", "hiking"],
      "dealbreakers": ["early mornings", "long walks"]
    }
  ]
}
```

Allowed values: `pace` = `relaxed | balanced | packed`;
`interests` = `food, coffee, museums, hiking, nightlife, shopping, views, relax`;
`dealbreakers` = `early mornings, long walks, late nights, expensive meals`.

Response (times are **minutes since midnight**):

```json
{
  "priority": "Svara",
  "plans": [
    {
      "key": "fastest",
      "label": "⚡ Fastest",
      "note": "One or two sentences explaining the tradeoff (LLM-written).",
      "days": [
        { "stops": [
          {
            "name": "Tartine Bakery", "area": "Mission", "emoji": "☕",
            "kind": "activity",
            "start": 644, "end": 689, "cost": 12,
            "travel_min": 14, "travel_how": "transit",
            "matches": ["Nehmat", "Svara"],
            "why": "Picked for Nehmat & Svara.", "tag": null
          }
        ] }
      ],
      "sc": {
        "travel": 149, "dwell": 670, "cost": 152,
        "sat": { "Nehmat": 61, "Svara": 87 },
        "minSat": 61, "meanSat": 74
      }
    }
  ]
}
```

- `key` is one of `fastest | max | cheapest`; return all three.
- `kind` is `meal` or `activity`; `tag` is `"Compromise"` or `null`; `travel_how` is `walk | transit`.
- `priority` is the traveler who gets the contested dinner pick (fairness memory), or `null`.
- `sc.sat` values are 0-100 per person; the UI highlights the lowest and best-in-row metrics.

## Memory shapes

`mem0Recall` → `{ "name": "Nehmat", "fields": { ...same keys as a member... }, "facts": ["Hated the 40-minute walk"] }`

Facts matching `/gave up|compromis/i` from a *previous* trip are what the mock planner uses for the dinner-pick priority.

## Demo flow

1. Pick a destination, add travelers. Nobody is remembered on a fresh backend, so the first trip starts from scratch.
2. Plan the trip. Conflict flags appear on the group profile; three plans come back with scorecards.
3. Switch the destination. Each traveler's form pre-fills from their last trip, badged `carried`; change a field and it is saved for that destination only.
4. Pick a plan and give feedback. Claude turns it into constraints, the re-plan shows what changed, and whoever the picks keep serving worst gets priority next time.