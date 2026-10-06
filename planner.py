"""Deterministic planner: merge traveler constraints, then greedy-insert stops under 3 weight profiles.

No LLM here. `adjustments` is how memory/feedback influences a plan (see ADJUSTMENT_KEYS).
"""
from math import asin, cos, radians, sin, sqrt

from data import fmt, to_min

# Each mode is a SELECTION objective over a pool of greedy itineraries (see plan()), not just a weight vector.
# This makes the headline metric hold by construction: Fastest has the smallest share of the trip spent travelling,
# Cheapest the lowest cost, Max Experience the most time at spots, among plans that clear the same floors.
MODES = ("fastest", "max_experience", "cheapest")
MODE_LABELS = {"fastest": "Fastest", "max_experience": "Max Experience", "cheapest": "Cheapest"}
HEADLINE = {"fastest": "least time spent travelling", "max_experience": "most time at spots", "cheapest": "lowest cost"}

# Greedy scoring weights tried for every pool member: marginal score =
#   w_fit*fit*dur_h + w_dwell*dur_h - w_travel*travel_h - w_cost*cost/10
POOL_WEIGHTS = [dict(w_fit=wf, w_dwell=wd, w_travel=wt, w_cost=wc, extra_stops=ex)
                for wf, wd, wt, wc, ex in [
                    (1.0, 0.6, 3.0, 0.1, 0), (1.0, 0.6, 6.0, 0.1, 0), (1.0, 0.3, 1.5, 0.1, 0), (1.0, 0.4, 3.0, 0.5, 1),
                    (2.0, 1.0, 0.5, 0.05, 1), (2.0, 1.5, 0.3, 0.05, 2), (1.5, 1.0, 1.0, 0.05, 1), (2.0, 1.0, 1.0, 0.1, 0),
                    (1.0, 0.3, 0.8, 1.5, 0), (1.0, 0.3, 1.5, 4.0, 0), (1.0, 0.5, 1.0, 1.5, 1), (1.0, 0.2, 3.0, 4.0, 0),
                    (1.0, 0.5, 1.0, 0.5, 0), (2.0, 0.6, 2.0, 1.0, 0), (1.0, 1.0, 2.0, 0.3, 1), (1.5, 0.4, 4.0, 2.0, 0)]]

# Everything tunable lives here; callers override per request via plan(..., settings={...}). Unknown keys are rejected.
DEFAULT_SETTINGS = dict(
    wake_buffer_min=30,          # group day starts this long after the latest waker
    budget_rule="min",           # hard cap per day: "min" (lowest budget in group), "median", or "none"
    pace_caps={"relaxed": 3, "moderate": 4, "active": 5},   # activities/day by pace; group takes the strictest
    max_stops_per_day=None,      # override pace caps entirely
    least_misery=0.5,            # 0 = optimise the group mean, 1 = optimise the worst-off traveler
    hard_fit=0.1,                # any traveler's fit below this excludes the place
    dealbreaker_rule="penalty",  # "penalty": a triggered dealbreaker cuts that traveler's fit; "exclude": never plan it
    long_walk_min=30,            # what "long walks" means: on-site walking minutes
    early_before="09:00",        # what "early mornings" means
    late_after="21:00",          # what "late nights" means (stop ends after this)
    expensive_meal_usd=25,       # what "expensive meals" means (or 25% of that traveler's daily budget if higher)
    min_activities_per_day=2,    # selection floor: no mode may win by being empty
    sat_slack=0.12,              # selection floor: worst-off traveler within this of the best plan in the pool
    max_day_travel_min=300,      # prefer plans with no day over this much travel (falls back if none qualify)
    share_gap=0.15,              # runner-up worse than this on a mode's metric -> share the best plan
    max_wait_min=60,             # max idle wait for a place to open
    lunch_window=["11:30", "14:30"], dinner_window=["17:30", "21:00"],
    day_start=None, day_end=None,                          # override the destination's day window ("HH:MM")
    pool_variants=5, min_eligible=4,                       # selection internals
)
_SETTING_TYPES = {k: type(v) for k, v in DEFAULT_SETTINGS.items() if v is not None}


def resolve_settings(settings: dict | None = None) -> dict:
    """Defaults + overrides. Raises ValueError on unknown keys or bad values so the UI can show the problem."""
    cfg = {**DEFAULT_SETTINGS, **{k: v for k, v in (settings or {}).items() if v is not None}}
    unknown = set(cfg) - set(DEFAULT_SETTINGS)
    if unknown:
        raise ValueError(f"unknown settings: {sorted(unknown)}; valid: {sorted(DEFAULT_SETTINGS)}")
    for k, t in _SETTING_TYPES.items():
        if t is float or t is int:
            if not isinstance(cfg[k], (int, float)) or isinstance(cfg[k], bool):
                raise ValueError(f"setting {k} must be a number")
    if cfg["dealbreaker_rule"] not in ("penalty", "exclude"):
        raise ValueError("dealbreaker_rule must be penalty or exclude")
    if cfg["budget_rule"] not in ("min", "median", "none"):
        raise ValueError("budget_rule must be min, median or none")
    if not 0 <= cfg["least_misery"] <= 1:
        raise ValueError("least_misery must be between 0 and 1")
    return cfg

# adjustments (all optional): avoid_ids[], avoid_tags[], max_walk_min, max_stops_per_day,
# priority_person (str; fairness), lodging_id
ADJUSTMENT_KEYS = {"avoid_ids", "avoid_tags", "max_walk_min", "max_stops_per_day",
                   "priority_person", "lodging_id"}

# ---------- merge ----------

SUPPORTED_DIETS = {"vegetarian", "pescatarian"}   # the only diets place data can verify (place["diet_ok"])


def _rest(w: str) -> tuple[int, int]:
    a, b = w.split("-")
    return to_min(a), to_min(b)


def merge_profile(travelers: list[dict], destination: dict | None = None, adjustments: dict | None = None,
                  settings: dict | None = None) -> dict:
    """Hard constraints by intersection, plus a list of human-readable conflicts for the UI."""
    adj, cfg = adjustments or {}, resolve_settings(settings)
    wakes = {t["name"]: to_min(t["wake_time"]) for t in travelers}
    budgets = {t["name"]: t["budget_per_day"] for t in travelers}
    raw_diets = {t["diet"].lower() for t in travelers} - {"omnivore"}
    diets = sorted({"vegetarian" if d == "vegan" else d for d in raw_diets} & SUPPORTED_DIETS)
    unverifiable = sorted(raw_diets - SUPPORTED_DIETS - {"vegan"})
    earliest, latest = min(wakes, key=wakes.get), max(wakes, key=wakes.get)
    cap_person = min(budgets, key=budgets.get)
    vals = sorted(budgets.values())
    cap = {"min": vals[0], "median": vals[len(vals) // 2], "none": 10 ** 9}[cfg["budget_rule"]]
    day_start = max(wakes.values()) + cfg["wake_buffer_min"]
    if cfg["day_start"]:
        day_start = max(day_start, to_min(cfg["day_start"]))
    elif destination:
        day_start = max(day_start, destination["day_start_min"])
    rest = sorted({_rest(w) for t in travelers for w in t["rest_windows"]})
    pace = min(cfg["pace_caps"].get(t.get("pace", "moderate"), 4) for t in travelers)
    max_stops = adj.get("max_stops_per_day") or cfg["max_stops_per_day"] or pace

    conflicts = []
    if wakes[latest] - wakes[earliest] >= 120:
        conflicts.append(dict(type="wake_time", people=[earliest, latest], severity="high",
            message=f"{earliest} wakes at {fmt(wakes[earliest])} but {latest} at {fmt(wakes[latest])}. "
                    f"Group starts at {fmt(day_start)}, so dawn activities are off the table."))
    if cfg["budget_rule"] != "none" and max(budgets.values()) >= 1.5 * vals[0]:
        conflicts.append(dict(type="budget", people=[cap_person], severity="medium",
            message=f"Budget capped at ${cap}/day per person ({cfg['budget_rule']} of the group); "
                    f"others could spend up to ${max(budgets.values())}."))
    if len(diets) > 1 or (diets and any(t["diet"] == "omnivore" for t in travelers)):
        conflicts.append(dict(type="diet", people=[t["name"] for t in travelers if t["diet"] != "omnivore"],
            severity="medium", message=f"Every meal must work for: {', '.join(diets)}."))
    if unverifiable:
        conflicts.append(dict(type="diet_unverified", people=[t["name"] for t in travelers if t["diet"].lower() in unverifiable],
            severity="low", message=f"Place data cannot verify {', '.join(unverifiable)}; check menus before booking."))
    if "vegan" in raw_diets:
        conflicts.append(dict(type="diet_vegan", people=[t["name"] for t in travelers if t["diet"].lower() == "vegan"],
            severity="low", message="Vegan is planned as vegetarian-safe; confirm vegan options at each meal."))
    walkers = [t["name"] for t in travelers if any(_norm(d) == "long_walk" for d in t["dealbreakers"])]
    hikers = [t["name"] for t in travelers if "hiking" in t["interests"]]
    if walkers and hikers:
        conflicts.append(dict(type="walking", people=walkers + hikers, severity="medium",
            message=f"{', '.join(walkers)} avoid long walks but {', '.join(hikers)} want to hike; "
                    f"hikes will be limited and balanced against everyone's satisfaction."))
    if destination and day_start >= 9 * 60:
        dawn_tags = {tg for p in destination["places"] if p["best_time"] == "dawn" for tg in p["tags"]}
        for t in travelers:
            if wakes[t["name"]] <= 7 * 60 + 30 and dawn_tags & set(t["interests"]):
                conflicts.append(dict(type="dawn", people=[t["name"]], severity="high",
                    message=f"{t['name']} would love dawn activities (wildlife, light), which are best early "
                            f"but the group does not start until {fmt(day_start)}."))

    return dict(
        names=[t["name"] for t in travelers], diets=diets, budget_cap=cap,
        budget_cap_person=cap_person if cfg["budget_rule"] == "min" else None, day_start_min=day_start, rest_windows=rest,
        max_stops_per_day=max_stops, conflicts=conflicts,
    )


# ---------- satisfaction ----------

def _norm(s: str) -> str:
    s = s.strip().lower().replace(" ", "_")
    return s[:-1] if s.endswith("s") else s


def _dealbreaker_hit(d_n: str, t: dict, place: dict, arrival: int, cfg: dict) -> float | None:
    """Penalty multiplier if dealbreaker `d_n` (normalised) is triggered by this stop, else None."""
    tags = {_norm(x) for x in place["tags"]}
    end = arrival + place["duration_min"]
    if d_n == "long_walk":
        return 0.3 if place["walk_min"] > cfg["long_walk_min"] else None
    if d_n == "early_morning":
        return 0.2 if arrival < to_min(cfg["early_before"]) else None
    if d_n == "late_night":
        return 0.2 if end > to_min(cfg["late_after"]) else None
    if d_n == "expensive_meal":
        return 0.2 if place.get("meal") and place["cost"] > max(cfg["expensive_meal_usd"], 0.25 * t["budget_per_day"]) else None
    if d_n == "chain_restaurant":
        return 0.1 if "chain" in tags else None
    return 0.2 if d_n in tags else None      # free-form dealbreaker that names a place tag, e.g. "tourist traps"


def person_fit(t: dict, place: dict, arrival: int, cfg: dict | None = None) -> float:
    cfg = cfg or DEFAULT_SETTINGS
    tags = set(place["tags"])
    matches = len(tags & set(t["interests"]))
    fit = 0.45 + 0.55 * min(1.0, 0.5 * matches)   # neutral stop = 0.45, two matching interests = 1.0
    if place.get("meal"):
        fit = max(fit, 0.5)                       # meals are never a miss on interests alone
    if place["cost"] > 0.5 * t["budget_per_day"]:
        fit *= 0.6
    for d in t["dealbreakers"]:
        if (m := _dealbreaker_hit(_norm(d), t, place, arrival, cfg)) is not None:
            fit *= m if cfg["dealbreaker_rule"] == "penalty" else 0.0
    return fit


# ---------- travel ----------

def _haversine_km(a, b):
    la1, lo1, la2, lo2 = map(radians, (a["lat"], a["lng"], b["lat"], b["lng"]))
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * asin(sqrt(h))


def travel_min(dest: dict, a: dict, b: dict) -> float:
    if a["id"] == b["id"]:
        return 0
    cfg = dest["travel"]
    key = f"{a['id']}|{b['id']}"
    if key in cfg.get("overrides", {}):
        return cfg["overrides"][key]
    return _haversine_km(a, b) * cfg["detour_factor"] / cfg["speed_kmh"] * 60 + 5


# ---------- optimizer ----------

def _diet_ok(place, diets):
    return not place.get("meal") or all(d in place["diet_ok"] for d in diets)


def _eligible(place, profile, adj):
    if place["id"] in adj.get("avoid_ids", []) or set(place["tags"]) & set(adj.get("avoid_tags", [])):
        return False
    if "max_walk_min" in adj and place["walk_min"] > adj["max_walk_min"]:
        return False
    return _diet_ok(place, profile["diets"])


def _plan_one(dest, travelers, profile, adj, wts, days, lodging, cfg, drop=frozenset()):
    pw = {t["name"]: (1.5 if t["name"] == adj.get("priority_person") else 1.0) for t in travelers}
    places = {p["id"]: p for p in dest["places"] if p["id"] not in drop and _eligible(p, profile, adj)}
    used, fits_all = set(), {t["name"]: [] for t in travelers}   # used: non-meal stops (never repeat)
    meal_visits = {}                                               # meal place id -> count (can repeat across days)
    cpm = dest["travel"].get("cost_per_min", 0)
    max_stops = profile["max_stops_per_day"] + wts["extra_stops"]
    MEALS = {"lunch": tuple(to_min(x) for x in cfg["lunch_window"]), "dinner": tuple(to_min(x) for x in cfg["dinner_window"])}
    FORCE_MEAL_AFTER = {m: w[0] - 30 for m, w in MEALS.items()}   # force the meal once its window is close
    MEAL_BUFFER, MAX_MEAL_WAIT = 60, 150
    day_end, rest = dest["day_end_min"], profile["rest_windows"]
    out_days = []

    def overlaps_rest(s, e):
        return any(s < re and e > rs for rs, re in rest)

    for day in range(1, days + 1):
        day_meals = set()
        t, pos, spent, stops, flags = profile["day_start_min"], lodging, 0.0, [], {"lunch": False, "dinner": False}
        day_travel = 0.0
        meal_exists = {m: any(p.get("meal") == m for p in places.values()) for m in MEALS}
        while True:
            for rs, re in rest:
                if rs <= t < re:
                    t = re
            cands = []
            for p in places.values():
                if p["id"] in (day_meals if p.get("meal") else used):
                    continue
                tr = travel_min(dest, pos, p)
                if spent + p["cost"] + tr * cpm > profile["budget_cap"]:
                    continue
                start = max(t + tr, p["open_min"])
                for rs, re in rest:            # a stop that would hit a rest window starts after it
                    if start < re and start + p["duration_min"] > rs:
                        start = re
                end = start + p["duration_min"]
                meal = p.get("meal")
                wait_cap = MAX_MEAL_WAIT if meal else cfg["max_wait_min"]
                if start - (t + tr) > wait_cap or end > p["close_min"] or end + travel_min(dest, p, lodging) > day_end:
                    continue
                if overlaps_rest(start, end):
                    continue
                if meal and (flags[meal] or not (MEALS[meal][0] <= start < MEALS[meal][1])):
                    continue
                if not meal and sum(1 for s in stops if not s["meal"]) >= max_stops:
                    continue
                if not meal and any(meal_exists[m] and not flags[m] and end > MEALS[m][1] - MEAL_BUFFER
                                    and start < MEALS[m][1] for m in MEALS):
                    continue
                fits = {x["name"]: person_fit(x, p, start, cfg) for x in travelers}
                if min(fits.values()) < cfg["hard_fit"]:
                    continue
                mean = sum(fits[n] * pw[n] for n in fits) / sum(pw.values())
                gfit = (1 - cfg["least_misery"]) * mean + cfg["least_misery"] * min(fits.values())
                dur_h = p["duration_min"] / 60
                score = (wts["w_fit"] * gfit * dur_h + wts["w_dwell"] * dur_h
                         - wts["w_travel"] * tr / 60 - wts["w_cost"] * (p["cost"] + tr * cpm) / 10
                         - 0.5 * meal_visits.get(p["id"], 0))
                cands.append((score, p, tr, start, end, fits))
            if not cands:
                break
            meals = [c for c in cands if c[1].get("meal")]
            forced = [c for c in meals if t >= FORCE_MEAL_AFTER[c[1]["meal"]]]
            n_act = sum(1 for s in stops if not s["meal"])
            non_meals = [c for c in cands if not c[1].get("meal") and (c[0] > 0 or n_act < cfg["min_activities_per_day"])]
            if forced:
                pick = max(forced, key=lambda c: c[0])
            elif non_meals:
                pick = max(non_meals, key=lambda c: c[0])
            elif meals:
                pick = max(meals, key=lambda c: c[0])
            else:
                break
            score, p, tr, start, end, fits = pick
            if p.get("meal"):
                flags[p["meal"]] = True
            if p.get("meal"):
                day_meals.add(p["id"])
                meal_visits[p["id"]] = meal_visits.get(p["id"], 0) + 1
            else:
                used.add(p["id"])
            spent += p["cost"] + tr * cpm
            day_travel += tr
            for n, f in fits.items():
                fits_all[n].append(f)
            stops.append(dict(place_id=p["id"], name=p["name"], meal=p.get("meal"), tags=p["tags"],
                              depart=fmt(t), arrive=fmt(t + tr), start=fmt(start), end=fmt(end),
                              travel_min=round(tr), dwell_min=p["duration_min"], cost=round(p["cost"] + tr * cpm, 2),
                              fit={n: round(f, 2) for n, f in fits.items()}))
            t, pos = end, p
        ret = travel_min(dest, pos, lodging) if stops else 0
        out_days.append(dict(day=day, stops=stops, travel_min=round(day_travel + ret), return_min=round(ret),
                             dwell_min=sum(s["dwell_min"] for s in stops), cost=round(spent + ret * cpm, 2),
                             back_at=fmt(t + ret) if stops else None))

    sat = {n: dict(mean=round(sum(v) / len(v), 2) if v else 0.0, min=round(min(v), 2) if v else 0.0)
           for n, v in fits_all.items()}
    total_cost = sum(d["cost"] for d in out_days)
    activities = sum(1 for d in out_days for st in d["stops"] if not st["meal"])
    travel_total, dwell_total = sum(d["travel_min"] for d in out_days), sum(d["dwell_min"] for d in out_days)
    return dict(
        days=out_days,
        scorecard=dict(
            activities=activities, travel_share=round(travel_total / max(1, travel_total + dwell_total), 3),
            max_day_travel=max((d["travel_min"] for d in out_days), default=0),
            travel_min=sum(d["travel_min"] for d in out_days), dwell_min=sum(d["dwell_min"] for d in out_days),
            total_cost=total_cost, cost_per_day=round(total_cost / days, 2),
            stops=sum(len(d["stops"]) for d in out_days), satisfaction=sat,
            min_satisfaction=min(s["min"] for s in sat.values()),
            mean_satisfaction=round(sum(s["mean"] for s in sat.values()) / len(sat), 2)),
    )


def _sig(it):
    return tuple(st["place_id"] for d in it["days"] for st in d["stops"])


# objective per mode: lower is better, computed on the finished itinerary's scorecard
OBJECTIVE = {
    "fastest": lambda sc: (sc["travel_share"], sc["travel_min"]),
    "cheapest": lambda sc: (sc["total_cost"], -sc["dwell_min"]),
    "max_experience": lambda sc: (-sc["dwell_min"], -sc["mean_satisfaction"]),
}


def _select(pool: list[dict], days: int, cfg: dict) -> dict:
    """Pick one pool plan per mode by its headline metric, among plans clearing the shared floors.
    Modes are filled cheapest, fastest, max so a tie never hands two modes the same itinerary."""
    best_min = max(it["scorecard"]["min_satisfaction"] for it in pool)
    tiers = [  # progressively relax the floors so there is always an answer
        lambda sc: sc["activities"] >= cfg["min_activities_per_day"] * days
        and sc["min_satisfaction"] >= best_min - cfg["sat_slack"] and sc["max_day_travel"] <= cfg["max_day_travel_min"],
        lambda sc: sc["activities"] >= cfg["min_activities_per_day"] * days and sc["min_satisfaction"] >= best_min - cfg["sat_slack"],
        lambda sc: sc["activities"] >= days,
        lambda sc: True,
    ]
    tiered = [[it for it in pool if t(it["scorecard"])] for t in tiers]
    # strictest tier that still leaves enough plans to choose between; otherwise the strictest non-empty one
    eligible = next((e for e in tiered if len(e) >= cfg["min_eligible"]), None) or next(e for e in tiered if e)
    out, taken = {}, set()
    # a plan can only serve one mode; fill modes in order of how much their best pick beats the field
    def margin(mode):
        vals = sorted(OBJECTIVE[mode](it["scorecard"])[0] for it in eligible)
        spread = (vals[-1] - vals[0]) or 1
        return (vals[1] - vals[0]) / spread if len(vals) > 1 else 0
    for mode in sorted(("cheapest", "fastest", "max_experience"), key=margin, reverse=True):
        ranked = sorted(eligible, key=lambda it: OBJECTIVE[mode](it["scorecard"]))
        pick = next((it for it in ranked if _sig(it) not in taken), ranked[0])
        best_v, pick_v = (OBJECTIVE[mode](it["scorecard"])[0] for it in (ranked[0], pick))
        shared = None
        if _sig(pick) != _sig(ranked[0]) and (pick_v - best_v) / (abs(best_v) or 1) > cfg["share_gap"]:
            # the runner-up would be clearly worse on this mode's own metric: honestly share the dominant plan instead
            pick = ranked[0]
            shared = next(m for m, it in out.items() if _sig(it) == _sig(pick))
        taken.add(_sig(pick))
        out[mode] = {**pick, "mode": mode, "label": MODE_LABELS[mode], "headline": HEADLINE[mode], "shared_with": shared}
    return {m: out[m] for m in MODES}


def plan(destination: dict, travelers: list[dict], days: int = 3, adjustments: dict | None = None,
         settings: dict | None = None) -> dict:
    """Returns {profile, itineraries: {mode: itinerary}, settings}. `settings` overrides DEFAULT_SETTINGS."""
    adj, cfg = adjustments or {}, resolve_settings(settings)
    if cfg["day_start"] or cfg["day_end"]:      # per-request day window without mutating the loaded destination
        destination = {**destination,
                       "day_start_min": to_min(cfg["day_start"]) if cfg["day_start"] else destination["day_start_min"],
                       "day_end_min": to_min(cfg["day_end"]) if cfg["day_end"] else destination["day_end_min"]}
    profile = merge_profile(travelers, destination, adj, cfg)
    lodgings = {l["id"]: l for l in destination["lodgings"]}
    lodging = lodgings.get(adj.get("lodging_id")) or destination["lodgings"][0]
    pool, seen = [], set()
    ids = [p["id"] for p in destination["places"] if not p.get("meal")]
    # variants: all places, then deterministic subsets with ~1 in 4 activities held out, for pool diversity
    drops = [frozenset()] + [frozenset(i for k, i in enumerate(ids) if (k * 7 + v * 3) % 4 == 0)
                             for v in range(1, cfg["pool_variants"])]
    for drop in drops:
        for w in POOL_WEIGHTS:
            it = _plan_one(destination, travelers, profile, adj, w, days, lodging, cfg, drop)
            if _sig(it) not in seen:    # duplicates add nothing to selection
                seen.add(_sig(it))
                pool.append(it)
    return dict(profile=profile, lodging=lodging["name"], days=days, pool_size=len(pool), settings=cfg,
                itineraries=_select(pool, days, cfg))
