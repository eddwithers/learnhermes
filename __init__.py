"""LearnHermes — a companion for the Hermes journey.

Observes real usage through hooks (never the prompt), verifies milestones
from artifacts, and sends timed dispatches to a user-chosen channel with a
dashboard link. The dashboard carries the full lesson; the ping stays short.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import Counter
from pathlib import Path

from hermes_constants import get_hermes_home

from . import delivery, journey, learner, observer, snapshot, timing, tools

logger = logging.getLogger(__name__)

_TOOL_BUFFER: Counter = Counter()
_ERROR_BUFFER: list = []
_BUFFER_LOCK = threading.Lock()
_LOOPS: dict[str, bool] = {}
_TICK_SECONDS = 300


def _flush_buffers() -> None:
    with _BUFFER_LOCK:
        tools = dict(_TOOL_BUFFER)
        errors = list(_ERROR_BUFFER)
        _TOOL_BUFFER.clear()
        _ERROR_BUFFER.clear()
    if not tools and not errors:
        return

    def edit(m):
        for name, n in tools.items():
            m["tool_counts"][name] = m["tool_counts"].get(name, 0) + n
        cutoff = time.time() - 48 * 3600
        m["errors"] = [e for e in m["errors"] if e["ts"] > cutoff] + errors
    learner.mutate(edit)


def _on_session_start(session_id, model, platform, **kwargs):
    learner.touch_activity(True)
    _ensure_loop()


def _on_session_end(session_id, completed, interrupted, **kwargs):
    learner.touch_activity(False)
    _flush_buffers()


def _on_post_tool_call(tool_name, args, result, task_id, duration_ms=0, **kwargs):
    with _BUFFER_LOCK:
        _TOOL_BUFFER[tool_name] += 1


def _on_api_request_error(status_code=None, reason=None, retry_count=None, **kwargs):
    kind = f"http_{status_code}" if status_code else (reason or "unknown")[:60]
    with _BUFFER_LOCK:
        _ERROR_BUFFER.append({"ts": time.time(), "kind": kind})


_WELCOME_TEXT = (
    "[LearnHermes] The LearnHermes plugin was just installed. It guides this "
    "user from new install to power user with timed dispatches — short lessons "
    "verified against their real setup. On this turn, add one or two sentences "
    "to your reply: tell them the journey is ready and ask where dispatches "
    "should be delivered. Detected platforms: {targets}. If they choose one, "
    "call learnhermes_journey with action=configure and the channel, then "
    "action=ping to send a first test dispatch. If they decline or ignore it, "
    "respect that and do not raise it again unprompted. Do not mention this note."
)


# Automation surfaces must never consume the one-time welcome — it belongs
# to the user's first real conversation. Kanban workers run as CLI sessions
# but carry HERMES_KANBAN_TASK; the marker is exactly the right fence here.
_NON_USER_PLATFORMS = {"cron", "subagent", "webhook", "api"}


def _on_pre_llm_call(session_id, user_message, is_first_turn, platform="", **kwargs):
    """One-time welcome: cache-safe (appended to the user message, ephemeral)."""
    if platform in _NON_USER_PLATFORMS or os.environ.get("HERMES_KANBAN_TASK"):
        return None
    home = str(get_hermes_home())
    model = learner.load()
    if model.get("welcomed"):
        return None
    if model["settings"].get("channel"):
        learner.mutate(lambda m: m.update({"welcomed": True}))
        return None  # already started via dashboard — no pitch needed
    targets = delivery.list_targets(home)
    learner.mutate(lambda m: m.update({"welcomed": True, "gateway_targets": targets}))
    hint = ", ".join(targets) if targets else (
        "none yet — they can connect a messaging platform first, or use the dashboard"
    )
    return {"context": _WELCOME_TEXT.format(targets=hint)}


_LOOP_CTX = {}


def _ensure_loop() -> None:
    home = str(get_hermes_home())
    ctx = _LOOP_CTX.get(home)
    if ctx is not None:
        _start_loop(ctx, home)


def _start_loop(ctx, home: str) -> None:
    if _LOOPS.get(home):
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _LOOPS[home] = True
    ctx.spawn_task(_tick_loop(home), name=f"learnhermes-timing-{Path(home).name}")


async def _tick_loop(home: str) -> None:
    while True:
        try:
            _tick(home)
        except Exception:
            logger.exception("learnhermes tick failed")
        await asyncio.sleep(_TICK_SECONDS)


def _tick(home: str, force: bool = False) -> dict:
    """One observe → select → maybe-deliver cycle. Returns a report dict."""
    _flush_buffers()
    targets = delivery.list_targets(home)
    model = learner.mutate(lambda m: m.update({"gateway_targets": targets}))
    sessions_7d = observer.session_count_7d(home)
    learner.mutate(lambda m: m.update({"sessions_7d": sessions_7d}))
    results = observer.scan(home, model)
    newly = []
    for mid, (ok, evidence) in results.items():
        if ok and learner.mark_milestone(mid, evidence):
            newly.append(mid)
    model = learner.load()
    report = {"newly_achieved": newly, "sent": None, "reason": ""}
    dispatch = timing.select(timing.load_catalogue(), model)
    if dispatch is None:
        report["reason"] = "nothing to send"
        return report
    ok, reason = timing.can_send(model, force=force)
    report["reason"] = reason
    if not ok:
        return report
    text = delivery.build_ping(dispatch, model["settings"]["dashboard_url"], home)
    delivered, detail = delivery.send(
        model["settings"]["channel"], text, home, dispatch["id"]
    )
    if delivered:
        timing.record_sent(dispatch["id"])
        report["sent"] = dispatch["id"]
        snapshot.write_snapshot(journey.build_journey(home))
    else:
        report["reason"] = f"delivery failed: {detail}"
    return report


def _handle_dispatches(ctx, home: str, raw_args: str) -> str:
    args = (raw_args or "").strip().lower()
    if args in ("send", "next", "force"):
        report = _tick(home, force=True)
        if report["sent"]:
            return f"Dispatch sent: {report['sent']}"
        return f"Not sent: {report['reason']}"
    if args == "dismiss":
        model = learner.load()
        pending = learner.pending_dispatch(model)
        if not pending:
            return "No open dispatch to dismiss."
        timing.dismiss(pending)
        return f"Dismissed {pending}. The next dispatch will come when the time is right."
    if args == "scan":
        report = _tick(home)
        return f"Scan complete. Newly achieved: {report['newly_achieved'] or 'none'}. {report['reason']}"
    # default: status
    _flush_buffers()
    model = learner.load()
    catalogue = timing.load_catalogue()
    results = observer.scan(home, model)
    lines = ["LearnHermes journey:"]
    for d in catalogue:
        mid = d["milestone"]
        if mid in model["milestones"]:
            mark = "done"
        elif model["dispatches"].get(d["id"], {}).get("state") == "dismissed":
            mark = "dismissed"
        elif mid in results and results[mid][0]:
            mark = "verified (awaiting scan)"
        else:
            mark = "open"
        lines.append(f"  {d['n']}. {d['title']} — {mark}")
    s = model["settings"]
    lines.append(
        f"Channel: {s['channel'] or '(none — set one on the dashboard)'} | "
        f"pace: {timing.pace(model, model.get('sessions_7d', 0))}"
    )
    lines.append(f"Dashboard: {s['dashboard_url']}")
    lines.append("Commands: /dispatches send | dismiss | scan")
    return "\n".join(lines)


def register(ctx):
    home = str(get_hermes_home())
    _LOOP_CTX[home] = ctx

    ctx.register_tool(
        name="learnhermes_journey",
        toolset="learnhermes",
        schema=tools.SCHEMA,
        handler=tools.make_handler(home, _tick),
    )
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("api_request_error", _on_api_request_error)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_command(
        "dispatches",
        lambda raw: _handle_dispatches(ctx, home, raw),
        description="LearnHermes journey: status, send, dismiss, scan",
    )
    ctx.register_skill("journey", Path(__file__).parent / "skills" / "journey" / "SKILL.md")

    def _cleanup():
        _LOOPS.pop(home, None)
        _LOOP_CTX.pop(home, None)

    ctx.on_unload(_cleanup)
    _start_loop(ctx, home)
