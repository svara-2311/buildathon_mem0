GroupTrip: front end
Shared trip room where each traveler adds preferences, the app merges them into a group
profile (with conflict flags), and an agent returns three plans (Fastest / Max Experience /
Cheapest) with scorecards. Feedback is written to memory and the trip is re-planned.
Single static file, no build step.
python3 -m http.server 8000   # then open <http://localhost:8000>
​
Where the backend plugs in
Everything the UI needs from the backend goes through three functions near the top of the
<script> in index.html. Each has the real fetch call sketched in a comment right above the mock.
Function
Replace with
Notes
planTrip(payload)
POST /api/plan
Optimizer + LLM explanation. Contract below.
mem0Recall(name)
GET /api/memory/recall?user_id=
Returns remembered profile fields + facts, or null. Drives the "from past trips" badges.
mem0Add(name, text, meta) / mem0SaveProfile(m) / mem0AddGroup(code, text)
POST /api/memory/add
Feedback, fairness ("compromised") and group-pick writes.
The mock optimizer (mockPlan) and seeded SF places (PLACES) can be deleted once planTrip calls the real API.
POST /api/plan
Request:
{
  "room": { "code": "AB12C", "destination": "San Francisco", "days": 2, "start_date": null, "lodging": "Mission District" },
  "members": [
    {
      "name": "Priya",
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
​
Allowed values: pace = relaxed | balanced | packed;
interests = food, coffee, museums, hiking, nightlife, shopping, views, relax;
dealbreakers = early mornings, long walks, late nights, expensive meals.
Response (times are minutes since midnight):
{
  "priority": "Sam",
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
            "matches": ["Priya", "Sam"],
            "why": "Picked for Priya & Sam.", "tag": null
          }
        ] }
      ],
      "sc": {
        "travel": 149, "dwell": 670, "cost": 152,
        "sat": { "Priya": 61, "Sam": 87 },
        "minSat": 61, "meanSat": 74
      }
    }
  ]
}
​
key is one of fastest | max | cheapest; return all three.
kind is meal or activity; tag is "Compromise" or null; travel_how is walk | transit.
priority is the traveler who gets the contested dinner pick (fairness memory), or null.
sc.sat values are 0-100 per person; the UI highlights the lowest and best-in-row metrics.
Memory shapes
mem0Recall → { "name": "Priya", "fields": { ...same keys as a member... }, "facts": ["Hated the 40-minute walk"] }
Facts matching /gave up|compromis/i from a previous trip are what the mock planner uses for the dinner-pick priority.
Demo script
Load demo room → Priya and Sam appear pre-filled with "from past trips" badges.
Add traveler → type Jordan → Autofill demo details (wakes at 10:00) → conflict flags appear on the group profile.
Plan our trip → three plans with scorecards. Tap a plan to see its timeline.
Give feedback ("Too much walking") → Re-plan → agent notes + memory update.
