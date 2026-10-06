"""Mem0 wrapper for the trip planner. Falls back to a local JSON store when MEM0_API_KEY is unset,
so the whole app runs offline (same interface either way).

Scopes (all just user_id strings in Mem0):
  personal  user_id="nehmat"                 revealed preferences, feedback, saved profile (pre-fill)
  group     user_id="group:friends-2026"     group behaviour, mode choices, fairness notes
  in-trip   metadata trip_id=...              live feedback that triggers a re-plan

Structured data rides in metadata as JSON strings (profile_json, adjustments_json) so planner inputs
stay deterministic; the natural-language text is what Mem0 searches over.
"""
import json
import os
import re
import time
import uuid
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_QUERY = "food diet wake-up time pace budget dealbreakers walking"
LOCAL_PATH = Path(__file__).parent / ".local_memory.json"


def user_id(name: str) -> str:
    return name.strip().lower()


class _LocalStore:
    """Tiny stand-in for MemoryClient: keyword-overlap search, newest first on ties."""

    def __init__(self, path: Path = LOCAL_PATH):
        self.path = path
        self.rows = json.loads(path.read_text()) if path.exists() else []

    def _save(self):
        self.path.write_text(json.dumps(self.rows, indent=1))

    def add(self, text, uid, metadata=None):
        row = dict(id=uuid.uuid4().hex, memory=text, user_id=uid, metadata=metadata or {}, created_at=time.time())
        self.rows.append(row)
        self._save()
        return row

    def get_all(self, uid):
        return sorted((r for r in self.rows if r["user_id"] == uid), key=lambda r: r["created_at"])

    def search(self, query, uid, top_k):
        q = set(re.findall(r"\w+", query.lower()))
        scored = [(len(q & set(re.findall(r"\w+", r["memory"].lower()))), r["created_at"], r) for r in self.get_all(uid)]
        return [r for _, _, r in sorted(scored, key=lambda x: (x[0], x[1]), reverse=True)[:top_k]]

    def clear(self, uid=None):
        self.rows = [r for r in self.rows if uid and r["user_id"] != uid]
        self._save()

    def reset(self):
        self.clear()


def _created(r) -> str:
    c = r.get("created_at", 0)
    return f"{c:020.3f}" if isinstance(c, (int, float)) else str(c)


class TripMemory:
    def __init__(self, local_path: Path | None = None):
        key = os.getenv("MEM0_API_KEY")
        if key and local_path is None:
            from mem0 import MemoryClient
            self.client, self.local, self.backend = MemoryClient(api_key=key), None, "mem0"
        else:
            self.client, self.local, self.backend = None, _LocalStore(local_path or LOCAL_PATH), "local"

    def reset(self):
        """Wipe memory. Local backend only; refuses to wipe a live Mem0 project."""
        if not self.local:
            raise PermissionError("refusing to wipe a live Mem0 project from the API")
        self.local.clear()

    # ---- raw ops ----
    def add(self, text: str, uid: str, metadata: dict | None = None, infer: bool = True):
        if self.local:
            return self.local.add(text, uid, metadata)
        # Mem0 2.x: identity goes inside filters, not as a top-level kwarg
        return self.client.add([{"role": "user", "content": text}], filters={"user_id": uid},
                               metadata=metadata or {}, infer=infer)

    def get_all(self, uid: str) -> list[dict]:
        if self.local:
            return self.local.get_all(uid)
        rows = self.client.get_all(filters={"user_id": uid}, page_size=200).get("results", [])
        return sorted(rows, key=_created)

    def search(self, query: str, uid: str, top_k: int = 5) -> list[dict]:
        if self.local:
            return self.local.search(query, uid, top_k)
        return self.client.search(query, filters={"user_id": uid}, top_k=top_k).get("results", [])

    def _by_type(self, uid: str, mtype: str) -> list[dict]:
        return [r for r in self.get_all(uid) if (r.get("metadata") or {}).get("type") == mtype]

    # ---- personal ----

    def save_profile(self, name: str, profile: dict, trip_id: str | None = None, destination: str | None = None):
        """Persist a confirmed traveler form. Scoped to `destination` so a tweak made for one place does not
        silently rewrite another; with no destination it is a plain cross-trip profile."""
        where = f" for {destination}" if destination else ""
        text = (f"{name}{where}: wakes {profile['wake_time']}, {profile['diet']}, ${profile['budget_per_day']}/day, "
                f"{profile.get('pace', 'moderate')} pace, likes {', '.join(profile['interests'])}, "
                f"dealbreakers: {', '.join(profile['dealbreakers']) or 'none'}.")
        self.add(text, user_id(name), dict(type="profile", profile_json=json.dumps(profile),
                                           trip_id=trip_id, destination=destination), infer=False)

    def _profile_rows(self, name: str) -> list[dict]:
        return self._by_type(user_id(name), "profile")      # oldest -> newest

    def prefill(self, name: str, destination: str | None = None) -> dict | None:
        """What to pre-fill a form with, and where each field came from.

        Precedence: this destination's own last saved form wins field by field, otherwise the most recent form
        from any other destination carries over. Returns {profile, sources}, where each source is
        {destination, trip_id, scope} and scope is one of:
          "customised_here" - saved for this destination and different from what other trips had
          "this_destination" - only ever set here
          "carried"          - reused from another destination's trip
        """
        rows = self._profile_rows(name)
        if not rows:
            return None
        dest_of = lambda r: (r.get("metadata") or {}).get("destination")
        load = lambda r: json.loads(r["metadata"]["profile_json"])
        specific = next((r for r in reversed(rows) if destination and dest_of(r) == destination), None)
        other = next((r for r in reversed(rows) if not destination or dest_of(r) != destination), None)
        spec_p, other_p = (load(specific) if specific else {}), (load(other) if other else {})
        profile, sources = {}, {}
        for k in {**other_p, **spec_p}:
            if k in spec_p:
                scope = ("customised_here" if k in other_p and spec_p[k] != other_p[k]
                         else "this_destination" if k not in other_p else "carried")
                row = specific if scope != "carried" else other
                profile[k] = spec_p[k]
            else:
                scope, row, profile[k] = "carried", other, other_p[k]
            sources[k] = dict(destination=dest_of(row), trip_id=(row.get("metadata") or {}).get("trip_id"), scope=scope)
        return dict(profile=profile, sources=sources, customised_here=bool(specific),
                    carried_from=dest_of(other) if other and not specific else None,
                    customised_fields=sorted(k for k, s in sources.items() if s["scope"] == "customised_here"))

    def prefill_profile(self, name: str, destination: str | None = None) -> dict | None:
        """Just the merged form values (see prefill for provenance)."""
        got = self.prefill(name, destination)
        return got["profile"] if got else None

    def recall(self, name: str, query: str = DEFAULT_QUERY, top_k: int = 5) -> list[str]:
        return [r["memory"] for r in self.search(query, user_id(name), top_k) if (r.get("metadata") or {}).get("type") != "profile"]

    def learn_feedback(self, name: str, text: str, trip_id: str, adjustments: dict | None = None):
        """In-trip / post-trip feedback. `adjustments` should come from explain.parse_feedback."""
        meta = dict(type="feedback", trip_id=trip_id)
        if adjustments:
            meta["adjustments_json"] = json.dumps(adjustments)
        self.add(text, user_id(name), meta)

    # ---- group ----
    def add_group_note(self, group_id: str, text: str, adjustments: dict | None = None, trip_id: str | None = None):
        meta = dict(type="group_pref", trip_id=trip_id)
        if adjustments:
            meta["adjustments_json"] = json.dumps(adjustments)
        self.add(text, group_id, meta)

    def record_mode_choice(self, group_id: str, mode: str, trip_id: str, satisfaction: dict | None = None):
        """The group's pick. `satisfaction` ({person: 0..1} of the chosen plan) is what makes fairness real:
        whoever the chosen plans keep serving worst accumulates a deficit and gets priority next time."""
        meta = dict(type="mode_choice", mode=mode, trip_id=trip_id)
        if satisfaction:
            meta["sat_json"] = json.dumps(satisfaction)
        self.add(f"The group picked the {mode} itinerary.", group_id, meta)

    def record_compromise(self, group_id: str, person: str, text: str, trip_id: str):
        """Explicit fairness event: `person` gave something up (counts toward their priority deficit)."""
        self.add(text, group_id, dict(type="fairness", person=person, trip_id=trip_id))

    def preferred_mode(self, group_id: str) -> str | None:
        modes = [r["metadata"]["mode"] for r in self._by_type(group_id, "mode_choice")]
        return Counter(modes).most_common(1)[0][0] if modes else None

    COMPROMISE_WEIGHT = 0.15     # one explicit compromise counts like a 15-point satisfaction shortfall
    PRIORITY_THRESHOLD = 0.04    # smallest accumulated deficit that earns priority

    def fairness_ledger(self, group_id: str) -> dict[str, float]:
        """person -> accumulated deficit. From chosen plans: (group mean satisfaction - person's) summed over picks,
        plus explicit compromises. Higher = has given up more = served first."""
        ledger: dict[str, float] = {}
        for r in self._by_type(group_id, "mode_choice"):
            if sat := (r.get("metadata") or {}).get("sat_json"):
                sat = json.loads(sat)
                mean = sum(sat.values()) / len(sat)
                for p, v in sat.items():
                    ledger[p] = ledger.get(p, 0.0) + (mean - v)
        for r in self._by_type(group_id, "fairness"):
            p = r["metadata"]["person"]
            ledger[p] = ledger.get(p, 0.0) + self.COMPROMISE_WEIGHT
        return {p: round(v, 3) for p, v in ledger.items()}

    def fairness_priority(self, group_id: str, names: list[str] | None = None) -> str | None:
        ledger = {p: v for p, v in self.fairness_ledger(group_id).items()
                  if not names or user_id(p) in {user_id(n) for n in names}}
        if not ledger:
            return None
        person, deficit = max(ledger.items(), key=lambda kv: kv[1])
        return person if deficit >= self.PRIORITY_THRESHOLD else None

    # ---- planner bridge ----
    def derive_adjustments(self, names: list[str], group_id: str | None) -> dict:
        """Merge stored adjustments (oldest to newest, so recent beats old) plus fairness priority.
        Feed straight into planner.plan(adjustments=...)."""
        rows = []
        for uid in [user_id(n) for n in names] + ([group_id] if group_id else []):
            rows += [r for r in self.get_all(uid) if (r.get("metadata") or {}).get("adjustments_json")]
        adj: dict = {}
        for r in sorted(rows, key=_created):
            a = json.loads(r["metadata"]["adjustments_json"])
            for k in ("avoid_ids", "avoid_tags"):
                adj[k] = sorted(set(adj.get(k, [])) | set(a.get(k, [])))
            for k in ("max_walk_min", "max_stops_per_day", "lodging_id"):
                if k in a:
                    adj[k] = a[k]
        if group_id and (p := self.fairness_priority(group_id, names)):
            adj["priority_person"] = next((n for n in names if user_id(n) == user_id(p)), None)
        return {k: v for k, v in adj.items() if v not in (None, [], "")}

    def recall_for_plan(self, names: list[str], group_id: str | None) -> dict:
        """Human-readable memories to show in the UI and feed to the explainer."""
        out = {n: self.recall(n) for n in names}
        if group_id:
            out["group"] = [r["memory"] for r in self.get_all(group_id)][-6:]
        return out
