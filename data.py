"""Loading and validating destination and persona JSON. Adding a destination = dropping a JSON file in destinations/."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent
DEST_DIR = ROOT / "destinations"

PLACE_KEYS = {"id", "name", "lat", "lng", "cost", "duration_min", "tags", "hours", "walk_min"}
TRAVELER_KEYS = {"name", "wake_time", "diet", "budget_per_day", "interests", "dealbreakers"}


def to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def fmt(minutes: float) -> str:
    minutes = int(round(minutes)) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def list_destinations() -> list[dict]:
    """[{name, label}] for the destination dropdown."""
    out = []
    for f in sorted(DEST_DIR.glob("*.json")):
        d = json.loads(f.read_text())
        out.append({"name": d["name"], "label": d["label"]})
    return out


def read_destination_raw(name: str) -> dict:
    path = DEST_DIR / f"{name}.json"
    if not path.exists() or not re.fullmatch(r"[a-z0-9_-]+", name):
        raise KeyError(name)
    return json.loads(path.read_text())


def validate_destination(d: dict) -> None:
    """Raises ValueError with a message the UI can show. Checks structure and cross-references."""
    for k in ("name", "label", "travel", "day_window", "lodgings", "places"):
        if k not in d:
            raise ValueError(f"destination missing '{k}'")
    if not d["lodgings"]:
        raise ValueError("destination needs at least one lodging")
    if not re.fullmatch(r"[a-z0-9_-]+", d["name"]):
        raise ValueError("destination name must be lowercase letters, digits, - or _")
    ids = set()
    for p in d["places"]:
        missing = PLACE_KEYS - p.keys()
        if missing:
            raise ValueError(f"place {p.get('id')} missing {sorted(missing)}")
        if p["id"] in ids:
            raise ValueError(f"duplicate place id {p['id']}")
        ids.add(p["id"])
        if len(p["hours"]) != 2 or p["duration_min"] <= 0:
            raise ValueError(f"place {p['id']}: bad hours or duration")
    for key in d["travel"].get("overrides", {}):
        a, _, b = key.partition("|")
        if not b:
            raise ValueError(f"travel override '{key}' must look like 'place_a|place_b'")
    for k in ("speed_kmh", "detour_factor"):
        if d["travel"].get(k, 1) <= 0:
            raise ValueError(f"travel.{k} must be positive")


def write_destination_raw(d: dict) -> None:
    validate_destination(d)
    DEST_DIR.mkdir(exist_ok=True)
    (DEST_DIR / f"{d['name']}.json").write_text(json.dumps(d, indent=1))


def enrich_destination(raw: dict) -> dict:
    """Adds computed minute fields; never written back to disk."""
    d = json.loads(json.dumps(raw))
    validate_destination(d)
    for p in d["places"]:
        p.setdefault("best_time", "any")
        p["open_min"] = to_min(p["hours"][0])
        close = to_min(p["hours"][1])
        p["close_min"] = close if close > p["open_min"] else close + 24 * 60  # overnight hours
    d["day_start_min"] = to_min(d["day_window"][0])
    d["day_end_min"] = to_min(d["day_window"][1])
    return d


def load_destination(name: str) -> dict:
    return enrich_destination(read_destination_raw(name))


def load_personas() -> list[dict]:
    people = json.loads((ROOT / "personas.json").read_text())
    for p in people:
        validate_traveler(p)
    return people


def validate_traveler(t: dict) -> dict:
    missing = TRAVELER_KEYS - t.keys()
    if missing:
        raise ValueError(f"traveler {t.get('name')} missing {missing}")
    t.setdefault("rest_windows", [])
    t.setdefault("pace", "moderate")
    return t
