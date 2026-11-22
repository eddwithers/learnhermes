"""The conversational surface — one tool the agent uses to run the journey.

Deliberately a single tool with an action enum: minimal schema footprint,
one place the model looks. Handlers return JSON strings and never raise.
"""

from __future__ import annotations

import json

SCHEMA = {
    "name": "learnhermes_journey",
    "description": (
        "LearnHermes journey companion — guides the user from new install to "
        "power user with timed dispatches (lessons verified against their real "
        "setup). When the user asks about LearnHermes, dispatches or their "
        "learning journey, call action=status FIRST and report their real "
        "progress plus the next_step guidance — never answer with a generic "
        "explanation alone. Also use when a LearnHermes welcome note asks you "
        "to offer setup. Actions: 'status' (journey, settings, detected "
        "channels, next_step), 'configure' (set channel and preferences), "
        "'dismiss' (skip the current dispatch), 'ping' (deliver the current "
        "dispatch now)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "configure", "dismiss", "ping"],
                "description": "What to do.",
            },
            "channel": {
                "type": "string",
                "description": "configure: any 'hermes send' target, e.g. telegram, discord:#channel, push, email. Empty string clears it.",
            },
            "enabled": {"type": "boolean", "description": "configure: pause/resume dispatches."},
            "send_start": {"type": "integer", "description": "configure: hour (0-23) dispatches may start arriving."},
            "send_end": {"type": "integer", "description": "configure: hour (0-23) dispatches stop arriving. May wrap midnight."},
            "max_per_day": {"type": "integer", "description": "configure: max dispatches per day (1-10)."},
            "dashboard_url": {"type": "string", "description": "configure: reachable dashboard URL used in ping links."},
        },
        "required": ["action"],
    },
}

_CONFIG_RULES = {
    "enabled": lambda v: isinstance(v, bool),
    "channel": lambda v: isinstance(v, str) and len(v) < 120,
    "dashboard_url": lambda v: isinstance(v, str) and v.startswith(("http://", "https://")) and len(v) < 300,
    "send_start": lambda v: isinstance(v, int) and 0 <= v <= 23,
    "send_end": lambda v: isinstance(v, int) and 0 <= v <= 23,
    "max_per_day": lambda v: isinstance(v, int) and 1 <= v <= 10,
}


def _next_step(j: dict) -> str:
    """Concrete guidance so the agent surfaces blockers, not just facts."""
    s = j["settings"]
    if not s.get("enabled", True):
        return "Dispatches are paused. If the user wants them, action=configure with enabled=true."
    if not s.get("channel"):
        if j.get("targets_known", True):
            platforms = ", ".join(j.get("gateway_targets", [])) or "none detected"
        else:
            platforms = "not checked yet (run action=status again shortly)"
        return (
            "No delivery channel is configured, so no dispatches can arrive yet — "
            "this is the most common reason a user sees nothing. Detected platforms: "
            f"{platforms}. Ask the user where dispatches should go, then "
            "action=configure with their chosen channel, then action=ping to send "
            "the first one. If no platform is connected, offer the dashboard instead."
        )
    platforms = j.get("gateway_targets", [])
    channel = (s.get("channel") or "").strip()
    if channel and j.get("targets_known", True) and channel.split(":")[0].strip().lower() not in platforms:
        detected = ", ".join(platforms) or "none"
        return (
            f"The configured channel '{channel}' is not connected on this profile, so dispatches "
            f"cannot arrive. Detected platforms: {detected}. Ask the user to pick a connected "
            "channel (action=configure), or clear the channel (empty string) for dashboard-only mode."
        )
    if j.get("current"):
        return (
            f"Current dispatch: {j['current']['title']}. It arrives at a natural "
            "pause inside the send window; action=ping sends it now."
        )
    return "All dispatches are complete or dismissed — the journey is done for now."


def make_handler(home: str, tick_fn):
    from . import journey, learner, timing

    def handler(args: dict, **kwargs) -> str:
        try:
            action = (args or {}).get("action", "status")
            if action == "status":
                j = journey.build_journey(home)
                from . import delivery
                return json.dumps({
                    "ok": True,
                    "milestones_done": j["milestones_done"],
                    "milestones_total": j["milestones_total"],
                    "pace": j["pace"],
                    "current_dispatch": j["current"],
                    "settings": j["settings"],
                    "detected_platforms": j["gateway_targets"],
                    # False = the platform list has never been read successfully;
                    # an empty detected_platforms then means "unknown", not "none".
                    "detected_platforms_known": j.get("targets_known", True),
                    "dashboard_link": delivery.dashboard_link(j["settings"]["dashboard_url"], home),
                    "next_step": _next_step(j),
                    "journey": [
                        {"n": d["n"], "title": d["title"], "state": d["state"]}
                        for d in j["dispatches"]
                    ],
                })
            if action == "configure":
                updates = {}
                for key, rule in _CONFIG_RULES.items():
                    if key in args:
                        if not rule(args[key]):
                            return json.dumps({"ok": False, "error": f"invalid value for {key}"})
                        updates[key] = args[key]
                if not updates:
                    return json.dumps({"ok": False, "error": "nothing to configure — pass channel, enabled, send_start, send_end, max_per_day or dashboard_url"})
                if "channel" in updates:
                    from . import delivery
                    ok, msg = delivery.validate_channel(updates["channel"], home)
                    if not ok:
                        return json.dumps({"ok": False, "error": msg})
                learner.mutate(lambda m: m["settings"].update(updates))
                return json.dumps({"ok": True, "written": updates, "settings": learner.load()["settings"]})
            if action == "dismiss":
                pending = learner.pending_dispatch(learner.load())
                if not pending:
                    return json.dumps({"ok": False, "error": "no open dispatch to dismiss"})
                timing.dismiss(pending)
                return json.dumps({"ok": True, "dismissed": pending})
            if action == "ping":
                report = tick_fn(home, force=True)
                ok = report["sent"] is not None
                return json.dumps({"ok": ok, "error": None if ok else report["reason"], **report})
            return json.dumps({"ok": False, "error": f"unknown action: {action}"})
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})

    return handler
