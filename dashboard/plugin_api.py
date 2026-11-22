"""LearnHermes dashboard API — mounted at /api/plugins/learnhermes/.

Imports the plugin as a package from its own directory so the same code
serves gateway hooks and dashboard routes. Plain def handlers: FastAPI runs
them in a worker thread, off the event loop.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT.parent))

import learnhermes as lh  # noqa: E402
from learnhermes import journey, learner, snapshot, timing  # noqa: E402

from fastapi import APIRouter  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from hermes_constants import get_hermes_home  # noqa: E402

router = APIRouter()

_SETTINGS_RULES = {
    "enabled": lambda v: isinstance(v, bool),
    "channel": lambda v: isinstance(v, str) and len(v) < 120,
    "dashboard_url": lambda v: isinstance(v, str) and v.startswith(("http://", "https://")) and len(v) < 300,
    "send_start": lambda v: isinstance(v, int) and 0 <= v <= 23,
    "send_end": lambda v: isinstance(v, int) and 0 <= v <= 23,
    "max_per_day": lambda v: isinstance(v, int) and 1 <= v <= 10,
}


@router.get("/journey")
def get_journey():
    return journey.build_journey(str(get_hermes_home()))


@router.get("/snapshot", response_class=HTMLResponse)
def get_snapshot():
    return snapshot.render_html(journey.build_journey(str(get_hermes_home())))


@router.post("/dispatch/{dispatch_id}/dismiss")
def dismiss(dispatch_id: str):
    known = {d["id"] for d in timing.load_catalogue()}
    if dispatch_id not in known:
        return {"ok": False, "error": "unknown dispatch"}
    timing.dismiss(dispatch_id)
    return {"ok": True}


@router.post("/settings")
def settings(body: dict):
    updates = {}
    for key, value in (body or {}).items():
        rule = _SETTINGS_RULES.get(key)
        if rule is None:
            return {"ok": False, "error": f"unknown setting: {key}"}
        if not rule(value):
            return {"ok": False, "error": f"invalid value for {key}"}
        updates[key] = value
    if not updates:
        return {"ok": False, "error": "nothing to update"}
    if "channel" in updates:
        # Same gate as the tool's configure action — the dashboard is the
        # other write path for channel and must not accept invented or
        # unconnected targets either.
        from learnhermes import delivery
        ok, msg = delivery.validate_channel(updates["channel"], str(get_hermes_home()))
        if not ok:
            return {"ok": False, "error": msg}

    def edit(m):
        m["settings"].update(updates)
    learner.mutate(edit)
    return {"ok": True, "written": list(updates)}


@router.post("/send-now")
def send_now():
    report = lh._tick(str(get_hermes_home()), force=True)
    ok = report["sent"] is not None
    # Surface the real reason (e.g. "no channel configured") — the UI shows
    # `error`, and a generic "action failed" hides the actual blocker.
    return {"ok": ok, "error": None if ok else report["reason"], **report}
