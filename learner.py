"""Learner model — durable, evidence-based progress for one profile.

Single JSON file under the plugin data dir. All writes go through mutate()
which holds an inter-process lock (flock where available) so the gateway
hooks, the timing loop and the dashboard API never lose updates.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path

from plugins.plugin_storage import plugin_data_dir

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

_LOCK = threading.RLock()

DEFAULT_MODEL = {
    "version": 1,
    "created_at": 0,
    "milestones": {},      # id -> {"achieved_at": ts, "evidence": str}
    "tool_counts": {},     # tool name -> observed call count
    "errors": [],          # [{"ts": ..., "kind": ...}] pruned to 48h
    "activity": {"last_activity": 0, "active": False},
    "dispatches": {},      # id -> {"state": pending|sent|done|dismissed, "sent_at": ts}
    "settings": {
        "enabled": True,
        "channel": "",     # any `hermes send` target, e.g. telegram, discord:#ops, push
        "dashboard_url": "http://127.0.0.1:9119/learnhermes",
        "send_start": 9,   # dispatches may arrive from this hour...
        "send_end": 20,    # ...until this hour (may wrap midnight)
        "max_per_day": 2,
    },
    "sent_today": {"date": "", "count": 0},
    "last_sent_at": 0,
}


def model_path() -> Path:
    return plugin_data_dir("learnhermes") / "learner.json"


def _read() -> dict:
    p = model_path()
    if not p.exists():
        m = copy.deepcopy(DEFAULT_MODEL)
        m["created_at"] = time.time()
        return m
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Malformed state is preserved beside the file, never silently reset.
        p.with_suffix(".corrupt").write_bytes(p.read_bytes())
        return copy.deepcopy(DEFAULT_MODEL)
    base = copy.deepcopy(DEFAULT_MODEL)
    base.update(data)
    stored = data.get("settings", {})
    # v0.1 migration: quiet_start/quiet_end actually described the send window.
    if "send_start" not in stored and "quiet_start" in stored:
        stored = {**stored, "send_start": stored["quiet_start"], "send_end": stored["quiet_end"]}
    base["settings"] = {**DEFAULT_MODEL["settings"], **stored}
    base["settings"].pop("quiet_start", None)
    base["settings"].pop("quiet_end", None)
    return base


def _write(model: dict) -> None:
    p = model_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(model, indent=2), encoding="utf-8")
    tmp.replace(p)


def load() -> dict:
    with _LOCK:
        return _read()


def mutate(fn) -> dict:
    """Read-modify-write under the inter-process lock. fn(model) edits in place."""
    with _LOCK:
        lock_path = model_path().with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a")
        try:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_EX)
            model = _read()
            fn(model)
            _write(model)
            return model
        finally:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


def record_tool_use(tool_name: str) -> None:
    def edit(m):
        m["tool_counts"][tool_name] = m["tool_counts"].get(tool_name, 0) + 1
        m["activity"]["last_activity"] = time.time()
    mutate(edit)


def record_error(kind: str) -> None:
    cutoff = time.time() - 48 * 3600

    def edit(m):
        m["errors"] = [e for e in m["errors"] if e["ts"] > cutoff]
        m["errors"].append({"ts": time.time(), "kind": kind})
    mutate(edit)


def touch_activity(active: bool) -> None:
    def edit(m):
        m["activity"]["last_activity"] = time.time()
        m["activity"]["active"] = active
    mutate(edit)


def mark_milestone(mid: str, evidence: str) -> bool:
    """Record a verified milestone. Returns True if newly achieved."""
    newly = False

    def edit(m):
        nonlocal newly
        if mid not in m["milestones"]:
            m["milestones"][mid] = {"achieved_at": time.time(), "evidence": evidence}
            newly = True
        # Evidence-backed completion closes any open dispatch for it.
        d = m["dispatches"].get(mid)
        if d and d["state"] in ("pending", "sent"):
            d["state"] = "done"
            d["done_at"] = time.time()
    mutate(edit)
    return newly


def pending_dispatch(model: dict) -> str | None:
    for did, d in model["dispatches"].items():
        if d["state"] in ("pending", "sent"):
            return did
    return None
