"""LLM layer: language only (parse feedback into planner adjustments, explain tradeoffs). No itinerary math here.
Every function has a deterministic fallback so the app still works without ANTHROPIC_API_KEY."""
import json
import os
import re

from dotenv import load_dotenv

from planner import MODE_LABELS

load_dotenv()
MODEL = "claude-sonnet-5-5"


LAST_ERROR: str | None = None     # why the most recent LLM call fell back; surfaced by /api/llm-check


def _ask(system: str, user: str, max_tokens: int = 1200) -> str | None:
    global LAST_ERROR
    if not os.getenv("ANTHROPIC_API_KEY"):
        LAST_ERROR = "ANTHROPIC_API_KEY is not set"
        return None
    try:
        import anthropic
        msg = anthropic.Anthropic().messages.create(
            model=MODEL, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}])
        LAST_ERROR = None
        return msg.content[0].text
    except Exception as e:  # network, auth, rate limit: degrade to the template
        LAST_ERROR = f"{type(e).__name__}: {e}"
        print(f"[explain] LLM call failed, using fallback: {LAST_ERROR}")
        return None


def _json(text: str | None) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


# ---------- feedback -> planner adjustments ----------

PARSE_SYSTEM = """You convert a traveler's free-text trip feedback into planner constraints.
Return ONLY a JSON object with any of these optional keys:
  "max_walk_min": int   (max on-site walking minutes per stop; use 40 for "too much walking", 20 for "no walking at all")
  "max_stops_per_day": int   ("too tired", "too packed" -> 2 or 3)
  "avoid_ids": [place ids]   (only ids from the provided list that the traveler wants removed)
  "avoid_tags": [tags]   (e.g. "hiking", "tourist_trap", "nightlife")
Omit keys that the feedback does not imply. No prose."""


def parse_feedback(text: str, destination: dict | None = None) -> dict:
    places = [{"id": p["id"], "name": p["name"]} for p in destination["places"]] if destination else []
    out = _json(_ask(PARSE_SYSTEM, f"Feedback: {text}\nPlaces: {json.dumps(places)}", 300))
    if out is None:
        out = _parse_fallback(text, destination)
    allowed = {"max_walk_min", "max_stops_per_day", "avoid_ids", "avoid_tags"}
    return {k: v for k, v in out.items() if k in allowed}


def _parse_fallback(text: str, destination: dict | None) -> dict:
    t, adj = text.lower(), {}
    if re.search(r"walk|hik|steep|uphill", t):
        adj["max_walk_min"] = 40
    if re.search(r"tired|exhaust|too packed|too many|rushed", t):
        adj["max_stops_per_day"] = 2
    if destination:
        ids = [p["id"] for p in destination["places"] if p["name"].lower() in t and re.search(r"skip|avoid|hate|swap|remove|cut", t)]
        if ids:
            adj["avoid_ids"] = ids
    return adj


# ---------- explanations ----------

EXPLAIN_SYSTEM = """You explain group-trip itineraries that a deterministic planner already produced.
Each plan's "headline" is the metric that mode optimizes (travel share = fraction of the trip spent travelling);
if "shared_with" is set, that mode and the named one are the same itinerary because it wins on both: say so.
Rules: use ONLY the numbers and facts provided; never invent places, times, or costs. Be concrete and brief
(2-3 sentences per mode). Name the tradeoff in minutes and dollars versus the other modes. If memories
influenced the plan (adjustments, fairness priority), say so by name and why. Return ONLY JSON:
{"overview": str, "modes": {"fastest": str, "max_experience": str, "cheapest": str}, "memory_notes": [str]}"""


def _compact(result: dict) -> dict:
    return {m: dict(headline=it["headline"], shared_with=it["shared_with"], scorecard=it["scorecard"],
                    days=[[s["name"] for s in d["stops"]] for d in it["days"]])
            for m, it in result["itineraries"].items()}


def explain_plans(result: dict, memories: dict | None = None, adjustments: dict | None = None) -> dict:
    """-> {"overview", "modes": {mode: text}, "memory_notes": [..]} ; conflicts are in result['profile']['conflicts']."""
    adj = adjustments or {}
    payload = dict(group=result["profile"], plans=_compact(result), memories=memories or {}, adjustments=adj)
    out = _json(_ask(EXPLAIN_SYSTEM, json.dumps(payload, default=str), 1500))
    if out and {"overview", "modes"} <= out.keys():
        out.setdefault("memory_notes", [])
        return out
    return _explain_fallback(result, adj)


def _explain_fallback(result: dict, adj: dict) -> dict:
    sc = {m: it["scorecard"] for m, it in result["itineraries"].items()}
    modes = {}
    for m, s in sc.items():
        others = [o for k, o in sc.items() if k != m]
        d_travel = s["travel_min"] - min(o["travel_min"] for o in others)
        d_cost = s["total_cost"] - min(o["total_cost"] for o in others)
        shared = result["itineraries"][m].get("shared_with")
        modes[m] = (f"{MODE_LABELS[m]} ({result['itineraries'][m]['headline']}){' - same plan as ' + MODE_LABELS[shared] + ', which wins on both' if shared else ''}: {s['stops']} stops, {s['travel_min']} min of travel, {s['dwell_min']} min at spots, "
                    f"${s['total_cost']:.0f} total per person. Worst-off traveler satisfaction {s['min_satisfaction']:.2f}. "
                    f"Compared with the best alternative: {d_travel:+d} min travel, {d_cost:+.0f} dollars.")
    notes = []
    if adj.get("priority_person"):
        notes.append(f"{adj['priority_person']} gets priority on contested choices because they compromised last time.")
    if "max_walk_min" in adj:
        notes.append(f"Stops with more than {adj['max_walk_min']} min of walking were removed because of earlier feedback.")
    if "max_stops_per_day" in adj:
        notes.append(f"Days are capped at {adj['max_stops_per_day']} activities, based on the group's history.")
    return dict(overview=f"Three plans for {result['days']} days staying at {result['lodging']}.", modes=modes, memory_notes=notes)


REPLAN_SYSTEM = """You explain what changed between two versions of a group itinerary and why.
Use ONLY the provided diff, feedback and memories. 2-4 sentences, mention people by name and cause and effect
(e.g. 'I reduced walking because X flagged it'). Return plain text."""


def diff_itineraries(before: dict, after: dict) -> dict:
    names = lambda it: {s["name"] for d in it["days"] for s in d["stops"]}
    b, a = names(before), names(after)
    sb, sa = before["scorecard"], after["scorecard"]
    return dict(removed=sorted(b - a), added=sorted(a - b),
                travel_delta=sa["travel_min"] - sb["travel_min"], cost_delta=round(sa["total_cost"] - sb["total_cost"], 2),
                min_satisfaction_delta=round(sa["min_satisfaction"] - sb["min_satisfaction"], 2))


def explain_replan(before: dict, after: dict, feedback: str, adjustments: dict, memories: dict | None = None) -> str:
    """before/after: the same mode's itinerary pre and post re-plan."""
    diff = diff_itineraries(before, after)
    payload = dict(mode=after["label"], feedback=feedback, diff=diff, adjustments=adjustments, memories=memories or {})
    text = _ask(REPLAN_SYSTEM, json.dumps(payload, default=str), 400)
    if text:
        return text.strip()
    parts = []
    if "max_walk_min" in adjustments:
        parts.append(f"I capped walking at {adjustments['max_walk_min']} min per stop because of your feedback ('{feedback}').")
    if adjustments.get("priority_person"):
        parts.append(f"{adjustments['priority_person']} gets priority on contested picks after compromising last time.")
    parts.append(f"Removed: {', '.join(diff['removed']) or 'nothing'}; added: {', '.join(diff['added']) or 'nothing'}. "
                 f"Travel {diff['travel_delta']:+d} min, cost {diff['cost_delta']:+.0f} dollars.")
    return " ".join(parts)
