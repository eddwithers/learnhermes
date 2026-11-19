"""Timing engine — right dispatch, right moment, never mid-task.

Rules (v0.1):
- one dispatch open at a time; the next waits for completion or dismissal
- delivery only at natural boundaries: user idle >= 15 min, no active session
- quiet hours and a daily cap, both user-configurable
- pace adapts: a fast-moving user gets shorter gaps between dispatches
- completion is evidence-based (observer), never self-report
"""

from __future__ import annotations

import time
from pathlib import Path

from ruamel.yaml import YAML

_CATALOGUE_PATH = Path(__file__).parent / "dispatches.yaml"
IDLE_SECONDS = 15 * 60

PACE_INTERVAL_HOURS = {"fast": 4, "steady": 12, "quiet": 24}


def load_catalogue() -> list[dict]:
    data = YAML(typ="safe").load(_CATALOGUE_PATH.read_text(encoding="utf-8"))
    return sorted(data["dispatches"], key=lambda d: d["n"])


def pace(model: dict, sessions_7d: int) -> str:
    achieved = len(model.get("milestones", {}))
    if sessions_7d >= 14 or achieved >= 5:
        return "fast"
    if sessions_7d >= 3:
        return "steady"
    return "quiet"


def select(catalogue: list[dict], model: dict) -> dict | None:
    """Next dispatch to send, or None if one is already open or all done."""
    from . import learner

    if learner.pending_dispatch(model):
        return None
    for d in catalogue:
        mid = d["milestone"]
        if mid in model.get("milestones", {}):
            continue
        state = model.get("dispatches", {}).get(d["id"], {}).get("state")
        if state in ("dismissed", "done"):
            continue
        return d
    return None


def in_send_window(settings: dict, hour: int) -> bool:
    """Window when dispatches may arrive. May wrap midnight (22 -> 6).
    Equal bounds = never send (fail closed on a misconfigured window)."""
    start, end = settings["send_start"], settings["send_end"]
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def can_send(model: dict, now: float | None = None, force: bool = False) -> tuple[bool, str]:
    now = now if now is not None else time.time()
    s = model["settings"]
    if not s.get("enabled", True):
        return False, "disabled"
    if not s.get("channel"):
        return False, "no channel configured"
    if force:
        return True, "forced"
    lt = time.localtime(now)
    if not in_send_window(s, lt.tm_hour):
        return False, "outside send window"
    today = time.strftime("%Y-%m-%d", lt)
    sent = model.get("sent_today", {})
    if sent.get("date") == today and sent.get("count", 0) >= s["max_per_day"]:
        return False, "daily cap reached"
    activity = model.get("activity", {})
    if activity.get("active"):
        return False, "session active"
    if now - activity.get("last_activity", 0) < IDLE_SECONDS:
        return False, "user not idle yet"
    interval_h = PACE_INTERVAL_HOURS[pace(model, model.get("sessions_7d", 0))]
    if now - model.get("last_sent_at", 0) < interval_h * 3600:
        return False, f"pace gap ({interval_h}h)"
    return True, "ok"


def record_sent(model: dict, dispatch_id: str, now: float | None = None) -> None:
    now = now if now is not None else time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))

    def edit(m):
        if m["sent_today"].get("date") != today:
            m["sent_today"] = {"date": today, "count": 0}
        m["sent_today"]["count"] += 1
        m["last_sent_at"] = now
        m["dispatches"][dispatch_id] = {"state": "sent", "sent_at": now}
    from . import learner
    learner.mutate(edit)


def dismiss(dispatch_id: str) -> None:
    from . import learner

    def edit(m):
        d = m["dispatches"].setdefault(dispatch_id, {})
        d["state"] = "dismissed"
        d["dismissed_at"] = time.time()
    learner.mutate(edit)
