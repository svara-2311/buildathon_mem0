"""HTTP adapter + static host for the GroupTrip UI. Translates the frontend contract to/from the planner,
memory and explain modules; no planning logic lives here.  Run: uvicorn server:app --port 8000"""
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

import explain
from data import (list_destinations, load_destination, load_personas, read_destination_raw, to_min,
                  validate_traveler, write_destination_raw)
from memory import TripMemory
from planner import ADJUSTMENT_KEYS, DEFAULT_SETTINGS, plan as run_plan
from seed_memory import seed

app = FastAPI(title="Group Trip Planner API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Never let memory setup kill the whole function: a bad MEM0_API_KEY or a read-only filesystem
# must degrade to local storage, not a 500 on every route. /health reports what happened.
MEM_ERROR = None
try:
    mem = TripMemory()
except Exception as e:                                  # noqa: BLE001 - any client/config failure
    MEM_ERROR = f"{type(e).__name__}: {e}"
    mem = TripMemory(local_path=Path(tempfile.gettempdir()) / "trip_memory.json")
_last_plan: dict[str, dict] = {}   # room code -> previous planner result, for re-plan diffs

# ---------- vocab translation (frontend <-> backend) ----------
PACE_IN = {"relaxed": "relaxed", "balanced": "moderate", "packed": "active"}
PACE_OUT = {v: k for k, v in PACE_IN.items()}
INTEREST_IN = {"views": "scenic", "relax": "coffee", "shopping": "shopping"}
INTEREST_OUT = {"scenic": "views", "sunsets": "views", "photography": "views", "wildlife": "hiking", "street_food": "food"}
UI_INTERESTS = {"food", "coffee", "museums", "hiking", "nightlife", "shopping", "views", "relax"}
UI_DEALBREAKERS = {"early mornings", "long walks", "late nights", "expensive meals"}
DIET_IN = {"vegan": "vegetarian", "veg": "vegetarian", "none": "omnivore", "": "omnivore"}
EMOJI = [("geysers", "♨️"), ("wildlife", "🦬"), ("hiking", "🥾"), ("museums", "🏛️"), ("coffee", "☕"),
         ("nightlife", "🌃"), ("sunsets", "🌅"), ("scenic", "🏞️"), ("street_food", "🌮"), ("food", "🍽️")]
PLAN_KEYS = {"fastest": "fastest", "max_experience": "max", "cheapest": "cheapest"}
PLAN_LABELS = {"fastest": "⚡ Fastest", "max": "✨ Max Experience", "cheapest": "💰 Cheapest"}


def traveler_in(m: dict) -> dict:
    diet = m.get("diet") or []
    diet = [diet] if isinstance(diet, str) else diet
    diet = [DIET_IN.get(d.lower(), d.lower()) for d in diet]
    diet = next((d for d in diet if d != "omnivore"), "omnivore")
    t = dict(name=m["name"], wake_time=m.get("wake_time", "09:00"), diet=diet,
             budget_per_day=m.get("budget_per_day", 100), pace=PACE_IN.get(m.get("pace", "balanced"), "moderate"),
             interests=[INTEREST_IN.get(i, i) for i in m.get("interests", [])],
             dealbreakers=m.get("dealbreakers", []), rest_windows=m.get("rest_windows", []))
    try:
        return validate_traveler(t)
    except ValueError as e:
        raise HTTPException(422, str(e))


def fields_out(t: dict) -> dict:
    interests = list(dict.fromkeys(INTEREST_OUT.get(i, i) for i in t["interests"]))
    return dict(wake_time=t["wake_time"], rest_windows=t.get("rest_windows", []),
                diet=[] if t["diet"] == "omnivore" else [t["diet"]], budget_per_day=t["budget_per_day"],
                pace=PACE_OUT.get(t.get("pace", "moderate"), "balanced"),
                interests=[i for i in interests if i in UI_INTERESTS],
                dealbreakers=[d for d in t.get("dealbreakers", []) if d in UI_DEALBREAKERS])


def resolve_destination(value: str) -> dict:
    v = (value or "").strip().lower()
    for d in list_destinations():
        if v in (d["name"], d["label"].lower()):
            return load_destination(d["name"])
    raise HTTPException(422, f"Unknown destination '{value}'. Options: {[d['name'] for d in list_destinations()]}")


def group_for(code: str | None) -> str | None:
    """Group memory is scoped to the room code, nothing else: a room only knows what it has actually done."""
    return f"group:{code.strip().lower()}" if code and code.strip() else None


def _emoji(tags):
    return next((e for k, e in EMOJI if k in tags), "📍")


# ---------- response shaping ----------

def plan_out(key: str, it: dict, dest: dict, priority: str | None, note: str, changes: dict | None):
    shared = PLAN_KEYS.get(it.get("shared_with") or "")
    days = []
    for d in it["days"]:
        stops = []
        for s in d["stops"]:
            matches = [n for n, f in s["fit"].items() if f >= 0.5]
            is_pick = bool(priority and s["meal"] == "dinner")
            stops.append(dict(
                name=s["name"], area="", emoji=_emoji(s["tags"]), kind="meal" if s["meal"] else "activity",
                start=to_min(s["start"]), end=to_min(s["end"]), cost=round(s["cost"]),
                travel_min=s["travel_min"], travel_how="walk" if dest["travel"]["mode"] != "driving" and s["travel_min"] <= 12 else "transit",  # UI only knows walk|transit
                matches=matches,
                why=(f"{priority} picks dinner after compromising last time." if is_pick
                     else f"Picked for {' & '.join(matches)}." if matches else "Fits the group's constraints."),
                tag="Compromise" if is_pick else None))
        days.append(dict(stops=stops))
    sc = it["scorecard"]
    sat = {n: round(v["mean"] * 100) for n, v in sc["satisfaction"].items()}
    return dict(key=key, label=PLAN_LABELS[key], note=note, days=days, changes=changes,
                headline=it["headline"], headline_metric=it["headline_metric"], shared_with=shared,
                sc=dict(travel=sc["travel_min"], dwell=sc["dwell_min"], cost=round(sc["total_cost"]), sat=sat,
                        minSat=min(sat.values()), meanSat=round(sum(sat.values()) / len(sat))))


# ---------- endpoints ----------

class PlanRequest(BaseModel):
    room: dict
    members: list[dict]
    settings: dict = {}            # planner knobs, see GET /api/settings
    remember: bool = True          # save each member's confirmed form, scoped to this destination
    ignore_memory: bool = False    # plan from the submitted forms alone, as if nothing was remembered
    adjustments: dict = {}         # explicit overrides of memory-derived constraints; null value clears one


@app.post("/api/plan")
def api_plan(req: PlanRequest):
    if not req.members:
        raise HTTPException(422, "need at least one member")
    room = req.room
    dest = resolve_destination(room.get("destination", ""))
    travelers = [traveler_in(m) for m in req.members]
    names = [t["name"] for t in travelers]
    code = room.get("code") or "room"
    gid = group_for(room.get("code"))
    derived = {} if req.ignore_memory else mem.derive_adjustments(names, gid)
    overridden = sorted(k for k, v in req.adjustments.items() if derived.get(k) != v)
    adj = {k: v for k, v in {**derived, **req.adjustments}.items() if v is not None}
    bad = set(adj) - ADJUSTMENT_KEYS
    if bad:
        raise HTTPException(422, f"unknown adjustments: {sorted(bad)}; valid: {sorted(ADJUSTMENT_KEYS)}")
    adj_run = dict(adj)
    lodging = next((l["id"] for l in dest["lodgings"] if room.get("lodging") and room["lodging"].lower() in l["name"].lower()), None)
    if lodging:
        adj_run["lodging_id"] = lodging
    try:
        result = run_plan(dest, travelers, int(room.get("days") or 3), adj_run, req.settings)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if req.remember:
        for t in travelers:
            if mem.prefill_profile(t["name"], dest["name"]) != t:
                mem.save_profile(t["name"], t, trip_id=code, destination=dest["name"])
    texts = explain.explain_plans(result, mem.recall_for_plan(names, gid), adj)
    prev = _last_plan.get(code)
    plans = []
    for mode, key in PLAN_KEYS.items():
        changes = explain.diff_itineraries(prev["itineraries"][mode], result["itineraries"][mode]) if prev else None
        plans.append(plan_out(key, result["itineraries"][mode], dest,
                              adj.get("priority_person"), texts["modes"].get(mode, ""), changes))
    _last_plan[code] = result
    return dict(priority=adj.get("priority_person"), plans=plans, conflicts=result["profile"]["conflicts"],
                fairness=mem.fairness_ledger(gid) if gid else {}, settings=result["settings"],
                adjustments=adj, adjustments_from_memory=derived, adjustments_overridden=overridden,
                agent_notes=texts.get("memory_notes", []), overview=texts.get("overview", ""),
                day_start=result["profile"]["day_start_min"])


@app.get("/api/memory/recall")
def api_recall(user_id: str, destination: str | None = None, room: str | None = None):
    """Pre-fill for this traveler at this destination. `sources` says per field whether the value was
    customised for this destination before or carried over from another trip, so the UI can badge it."""
    got = mem.prefill(user_id, destination)
    if not got:
        return None
    profile = got["profile"]
    facts = mem.recall(user_id)
    if gid := group_for(room):
        facts += [r["memory"] for r in mem.get_all(gid)
                  if (r.get("metadata") or {}).get("type") == "fairness"
                  and (r["metadata"].get("person") or "").lower() == user_id.lower()]
    return dict(name=profile["name"], fields=fields_out(profile), facts=facts, sources=got["sources"],
                customised_here=got["customised_here"], carried_from=got["carried_from"],
                customised_fields=got["customised_fields"])


class MemoryAdd(BaseModel):
    name: str | None = None
    user_id: str | None = None
    text: str = ""
    meta: dict = {}
    group: str | None = None


@app.post("/api/memory/add")
def api_memory_add(body: MemoryAdd):
    meta, who = body.meta or {}, body.name or body.user_id
    mtype = meta.get("type", "feedback")
    trip = meta.get("trip_id") or meta.get("room") or "trip"
    if mtype == "profile":
        profile = traveler_in({**meta.get("profile", {}), "name": who})
        mem.save_profile(who, profile, trip, destination=meta.get("destination"))
        return dict(ok=True)
    gid = group_for(meta.get("room"))
    if mtype in ("compromise", "fairness", "mode_choice", "pick", "group", "group_pref") and not gid:
        raise HTTPException(422, "meta.room (room code) is required for group-level memory")
    if mtype in ("compromise", "fairness"):
        mem.record_compromise(gid, who, body.text, trip)
        return dict(ok=True)
    if mtype in ("mode_choice", "pick"):
        ui_mode = meta.get("mode", "")
        mode = next((m for m, k in PLAN_KEYS.items() if k == ui_mode or m == ui_mode), None)
        prev = _last_plan.get(meta.get("room"))
        if not mode or not prev:
            raise HTTPException(422, "mode_choice needs meta.mode (fastest|max|cheapest) and a plan made for this room")
        sat = {n: v["mean"] for n, v in prev["itineraries"][mode]["scorecard"]["satisfaction"].items()}
        mem.record_mode_choice(gid, mode, trip, sat)
        return dict(ok=True, fairness=mem.fairness_ledger(gid), priority=mem.fairness_priority(gid, list(sat)))
    if mtype in ("group", "group_pref"):
        mem.add_group_note(gid, body.text, trip_id=trip)
        return dict(ok=True)
    if not who:
        raise HTTPException(422, "name required for feedback")
    dest = None
    if meta.get("destination"):
        dest = resolve_destination(meta["destination"])
    adjustments = explain.parse_feedback(body.text, dest)
    mem.learn_feedback(who, body.text, trip, adjustments)
    return dict(ok=True, adjustments=adjustments)


@app.get("/api/destinations")
def api_destinations():
    return list_destinations()


@app.get("/api/personas")
def api_personas():
    return [dict(name=p["name"], fields=fields_out(p), known=mem.prefill_profile(p["name"]) is not None)
            for p in load_personas()]


@app.post("/api/reset")
def api_reset():
    """Start clean: wipe local memory and cached plans. (Refuses on a live Mem0 project.)"""
    try:
        mem.reset()
    except PermissionError as e:
        raise HTTPException(403, str(e))
    _last_plan.clear()
    return dict(ok=True, backend=mem.backend)


@app.post("/api/seed")
def api_seed():
    """OPTIONAL sample history (fictional past trips) for dry runs. Not used by default; nothing depends on it."""
    seed(mem)
    return dict(ok=True, backend=mem.backend, note="sample history loaded under group 'group:friends-2026'")


@app.get("/api/llm-check", include_in_schema=False)
def llm_check():
    """Diagnostic: makes one tiny Claude call and reports the real error if it fails, since explanations
    fall back to templates silently."""
    text = explain._ask("Reply with the single word: ok", "ping", max_tokens=8)
    return dict(ok=bool(text), model=explain.MODEL, reply=(text or "").strip()[:40],
                error=explain.LAST_ERROR, key_present=bool(os.getenv("ANTHROPIC_API_KEY")),
                key_prefix=(os.getenv("ANTHROPIC_API_KEY") or "")[:11] or None)


@app.get("/api/settings")
def api_settings():
    """Every planner knob with its default; pass overrides in POST /api/plan {settings:{...}}."""
    return DEFAULT_SETTINGS


# ---------- live-editable destination data ----------

def _raw(name: str) -> dict:
    try:
        return read_destination_raw(name)
    except KeyError:
        raise HTTPException(404, f"no destination '{name}'")


def _save(d: dict) -> dict:
    try:
        write_destination_raw(d)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except OSError as e:
        raise HTTPException(503, f"destination files are read-only here ({e.strerror}); edit them locally")
    return d


@app.get("/api/destinations/{name}")
def api_destination(name: str):
    return _raw(name)


@app.put("/api/destinations/{name}")
def api_put_destination(name: str, body: dict):
    """Create or replace a whole destination (same JSON as destinations/*.json)."""
    return _save({**body, "name": name})


@app.put("/api/destinations/{name}/places/{place_id}")
def api_put_place(name: str, place_id: str, body: dict):
    """Add or edit one place. Required: name, lat, lng, cost, duration_min, tags[], hours[open, close], walk_min.
    Optional: best_time (dawn|day|any), meal (lunch|dinner) + diet_ok[]."""
    d = _raw(name)
    place = {**body, "id": place_id}
    d["places"] = [p for p in d["places"] if p["id"] != place_id] + [place]
    _save(d)
    return place


@app.delete("/api/destinations/{name}/places/{place_id}")
def api_delete_place(name: str, place_id: str):
    d = _raw(name)
    if place_id not in {p["id"] for p in d["places"]}:
        raise HTTPException(404, f"no place '{place_id}'")
    d["places"] = [p for p in d["places"] if p["id"] != place_id]
    _save(d)
    return dict(ok=True)


@app.put("/api/destinations/{name}/lodgings/{lodging_id}")
def api_put_lodging(name: str, lodging_id: str, body: dict):
    d = _raw(name)
    lodging = {**body, "id": lodging_id}
    d["lodgings"] = [x for x in d["lodgings"] if x["id"] != lodging_id] + [lodging]
    _save(d)
    return lodging


@app.patch("/api/destinations/{name}/travel")
def api_patch_travel(name: str, body: dict):
    """Edit travel model: speed_kmh, detour_factor, cost_per_min, overrides {'a|b': minutes}."""
    d = _raw(name)
    d["travel"] = {**d["travel"], **body}
    _save(d)
    return d["travel"]


# ---------- static UI (same origin as the API, so no CORS in the browser) ----------
FRONTEND = Path(__file__).parent / "frontend"


@app.get("/", include_in_schema=False)
def index():
    page = FRONTEND / "index.html"
    if not page.exists():
        raise HTTPException(503, f"UI not bundled with this deployment (looked in {FRONTEND})")
    return FileResponse(page)


@app.get("/health", include_in_schema=False)
def health():
    """Reports what is actually working, so a broken deployment says why instead of returning 500."""
    try:
        dests = [d["name"] for d in list_destinations()]
        dest_error = None
    except Exception as e:                              # noqa: BLE001
        dests, dest_error = [], f"{type(e).__name__}: {e}"
    return dict(ok=bool(dests) and not MEM_ERROR, backend=mem.backend, destinations=dests,
                memory_path=str(mem.local.path) if mem.local else None, memory_error=MEM_ERROR,
                destinations_error=dest_error, ui_bundled=(FRONTEND / "index.html").exists(),
                anthropic_key=bool(os.getenv("ANTHROPIC_API_KEY")), mem0_key=bool(os.getenv("MEM0_API_KEY")))
