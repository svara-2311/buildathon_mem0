"""Preload Mem0 so the memory features are visible in the demo. Run once: python seed_memory.py [--reset]"""
import sys

from data import load_personas
from memory import TripMemory, user_id

GROUP = "group:friends-2026"


def seed(mem: TripMemory, reset: bool = False):
    if reset and mem.local:
        mem.local.clear(GROUP)
        for n in ("nehmat", "svara", "aanchal"):
            mem.local.clear(n)
    people = {p["name"]: p for p in load_personas()}
    # Returning users: Nehmat and Svara. Aanchal is deliberately left new (joins live in the demo).
    for name in ("Nehmat", "Svara"):
        mem.save_profile(name, people[name], trip_id="portland-2025")
    mem.add("Always orders the vegetarian option. Sleeps in on day 2 of every trip and skips mornings.",
            user_id("Nehmat"), dict(type="behavior", trip_id="portland-2025"))
    mem.add("Loved the long coffee stop but the 40-minute uphill walk in Portland wore everyone out.",
            user_id("Nehmat"), dict(type="feedback", trip_id="portland-2025"))
    mem.add("Gets restless on low-activity days and happily takes early hikes. Hates overpriced tourist traps.",
            user_id("Svara"), dict(type="behavior", trip_id="portland-2025"))
    mem.add("The group tends to do about 3 activities a day and skips anything with more than ~45 minutes of transit.",
            GROUP, dict(type="group_pref", trip_id="portland-2025", adjustments_json='{"max_stops_per_day": 3}'))
    for mode in ("cheapest", "cheapest", "max_experience"):
        mem.record_mode_choice(GROUP, mode, trip_id="past")
    mem.record_compromise(GROUP, "Svara", "Svara gave up the dinner choice on Day 1 of the Portland trip so the group could "
                          "eat somewhere vegetarian-friendly.", trip_id="portland-2025")
    print(f"Seeded ({mem.backend} backend).")


if __name__ == "__main__":
    seed(TripMemory(), reset="--reset" in sys.argv)
