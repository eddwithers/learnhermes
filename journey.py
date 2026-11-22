"""Journey assembly — one source of truth for dashboard, snapshot and CLI."""

from __future__ import annotations

from . import learner, observer, timing


def build_journey(home: str) -> dict:
    model = learner.load()
    catalogue = timing.load_catalogue()
    results = observer.scan(home, model)
    rows = []
    for d in catalogue:
        mid = d["milestone"]
        achieved = model["milestones"].get(mid)
        dstate = model["dispatches"].get(d["id"], {}).get("state", "")
        if achieved:
            state = "done"
        elif dstate == "dismissed":
            state = "dismissed"
        elif dstate in ("sent", "sending"):
            state = "sent"
        else:
            state = "open"
        evidence = ""
        if achieved:
            evidence = achieved["evidence"]
        elif results.get(mid, (False, ""))[0]:
            evidence = results[mid][1]
        rows.append({
            **d,
            "state": state,
            "achieved_at": achieved["achieved_at"] if achieved else None,
            "evidence": evidence,
        })
    pending = learner.pending_dispatch(model)
    current = next((r for r in rows if r["id"] == pending), None) if pending else None
    if current is None:
        nxt = timing.select(catalogue, model)
        current = next((r for r in rows if r["id"] == nxt["id"]), None) if nxt else None
    return {
        "settings": model["settings"],
        "pace": timing.pace(model, model.get("sessions_7d", 0)),
        "sessions_7d": model.get("sessions_7d", 0),
        "activity": model.get("activity", {}),
        "errors_48h": len(model.get("errors", [])),
        "gateway_targets": model.get("gateway_targets", []),
        # False until a platform listing has succeeded at least once — then an
        # empty list means "none connected", not "never checked".
        "targets_known": "gateway_targets" in model,
        "dispatches": rows,
        "current": current,
        "milestones_done": len(model["milestones"]),
        "milestones_total": len(catalogue),
    }
