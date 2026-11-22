"""LearnHermes — a companion for the Hermes journey.

Observes real usage through hooks (never the prompt), verifies milestones
from artifacts, and sends timed dispatches to a user-chosen channel with a
dashboard link. The dashboard carries the full lesson; the ping stays short.
"""

from __future__ import annotations

import asyncio
import contextvars
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
_LOOPS: dict[str, threading.Event] = {}  # home -> stop event for the timing loop
_LOOPS_LOCK = threading.Lock()
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
    "should be delivered. Offer ONLY these detected platforms: {targets}. "
    "If they pick one, call learnhermes_journey action=configure with that "
    "channel, then action=ping to send a first test dispatch. If they say "
    "'here' / 'the dashboard' / want no messaging app, that is dashboard-only "
    "mode: leave the channel UNSET (do not configure a channel named "
    "'dashboard' or 'here' — those are not send targets) and tell them "
    "dispatches will wait on the LearnHermes dashboard to read. If no platform "
    "is detected, say they can connect one later or just use the dashboard. "
    "If they decline, respect it and do not raise it again unprompted. Never "
    "restart the gateway or run `hermes update` for LearnHermes. Do not mention "
    "this note."
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
    learner.mutate(lambda m: m.update(
        {"welcomed": True, **({"gateway_targets": targets} if targets is not None else {})}
    ))
    hint = ", ".join(targets) if targets else (
        "unknown right now (listing failed) — the user can check later, or use the dashboard"
        if targets is None else
        "none yet — they can connect a messaging platform first, or use the dashboard"
    )
    return {"context": _WELCOME_TEXT.format(targets=hint)}


_LOOP_CTX = {}


def _ensure_loop() -> None:
    home = str(get_hermes_home())
    ctx = _LOOP_CTX.get(home)
    if ctx is not None:
        _start_loop(ctx, home)


def _thread_loop(home: str, stop: threading.Event) -> None:
    """The timing loop on a plain daemon thread (the in-process path).

    Deliberately not asyncio: `asyncio.to_thread` runs work on the default
    executor, whose worker threads the interpreter joins at exit — a
    short-lived `hermes` command would hang until the wait ended. A plain
    daemon thread blocked in stop.wait() never delays process exit.

    The first tick waits a full interval, so a short-lived process exits
    long before it ticks — it neither scans nor attempts delivery. Only a
    process that stays up for a whole interval (the gateway, an open chat)
    ever ticks.
    """
    while not stop.wait(_TICK_SECONDS):
        try:
            _tick(home)
        except Exception:
            logger.exception("learnhermes tick failed")


def _should_arm() -> bool:
    """Never arm in a process LearnHermes itself spawned.

    Any `hermes` CLI invocation discovers and loads plugins. A child that LearnHermes
    spawned (`hermes send --list`, `hermes send`) must never arm: it would run its own
    loop alongside the parent's. Ordinary short-lived commands may arm, but the first
    tick waits a full interval, so they exit without ever ticking.
    """
    return os.environ.get(delivery.SUBPROCESS_ENV) != "1"


def _start_loop(ctx, home: str) -> None:
    """Arm the timing loop from ANY thread.

    register() never runs with an asyncio loop on its own thread: in the default
    in-process mode it runs on a plugin-load worker thread, and in the isolated
    plugin host on the channel request thread. The old get_running_loop() guard
    therefore skipped arming in both modes — after a gateway restart a quiet
    profile never ticked again, which is exactly when dispatches should arrive.

    ctx.spawn_task is tried first (the plugin host schedules it onto its own,
    long-lived loop); in-process it needs a running loop and raises, so a plain
    daemon thread runs the loop instead. That thread runs inside a copy of the
    caller's contextvars, so the profile-home override (a contextvar) follows
    it and state is written under the right profile. Unload sets the stop
    event in both modes (host-spawned tasks are not auto-cancelled).
    """
    if not _should_arm():
        return
    with _LOOPS_LOCK:
        stop = _LOOPS.get(home)
        if stop is not None and not stop.is_set():
            return
        stop = threading.Event()
        _LOOPS[home] = stop
    name = f"learnhermes-timing-{Path(home).name}"
    coro = _tick_loop(home, stop)
    try:
        ctx.spawn_task(coro, name=name)
    except Exception:
        coro.close()  # never awaited on this path
        scoped = contextvars.copy_context()
        threading.Thread(target=scoped.run, args=(_thread_loop, home, stop),
                         daemon=True, name=name).start()


async def _tick_loop(home: str, stop: threading.Event) -> None:
    """The timing loop on the plugin host's own event loop (spawn_task path).

    Same contract as _thread_loop: wait a full interval before each tick.
    The wait is asyncio.sleep in short steps, not stop.wait on the default
    executor: an executor thread parked for minutes would be joined at
    interpreter exit and delay it. Only the tick itself (synchronous file
    locks + hermes CLI subprocesses) runs off the loop, via to_thread.
    """
    while True:
        deadline = time.monotonic() + _TICK_SECONDS
        while not stop.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        if stop.is_set():
            return
        try:
            await asyncio.to_thread(_tick, home)
        except Exception:
            logger.exception("learnhermes tick failed")


def _tick(home: str, force: bool = False) -> dict:
    """One observe → select → maybe-deliver cycle. Returns a report dict.

    Serialised across processes: the gateway and an open `hermes chat` on the
    same profile can both run a timing loop, and select → send → record is not
    atomic, so two ticks at once could deliver the same dispatch twice. A
    non-blocking lock means the second tick simply skips this round.
    """
    fh = _try_tick_lock()
    if fh is None:
        return {"newly_achieved": [], "sent": None,
                "reason": "another LearnHermes tick is running on this profile; try again shortly"}
    try:
        return _tick_locked(home, force)
    finally:
        _release_tick_lock(fh)


def _try_tick_lock():
    from plugins.plugin_storage import plugin_data_dir
    path = plugin_data_dir("learnhermes") / "tick.lock"
    fh = open(path, "a+")
    try:
        if learner.fcntl is not None:
            learner.fcntl.flock(fh, learner.fcntl.LOCK_EX | learner.fcntl.LOCK_NB)
        elif learner.msvcrt is not None:
            fh.seek(0)
            learner.msvcrt.locking(fh.fileno(), learner.msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return None
    return fh


def _release_tick_lock(fh) -> None:
    try:
        if learner.fcntl is not None:
            learner.fcntl.flock(fh, learner.fcntl.LOCK_UN)
        elif learner.msvcrt is not None:
            fh.seek(0)
            learner.msvcrt.locking(fh.fileno(), learner.msvcrt.LK_UNLCK, 1)
    finally:
        fh.close()


def _tick_locked(home: str, force: bool) -> dict:
    _flush_buffers()
    timing.settle_stale_sends()
    listed = delivery.list_targets(home)
    if listed is not None:
        model = learner.mutate(lambda m: m.update({"gateway_targets": listed}))
    else:
        model = learner.load()  # listing failed: keep the last known targets
    targets = model.get("gateway_targets", [])
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
    # A saved channel that is not a connected platform on THIS profile can
    # never deliver (e.g. set by older code, or left behind after a platform
    # disconnect). Surface the blocker instead of failing silently every
    # tick — forced pings included, so the user gets the real reason.
    channel = (model["settings"].get("channel") or "").strip()
    if channel:
        platform = channel.split(":")[0].strip().lower()
        if listed is None:
            report["reason"] = (
                "couldn't list delivery platforms just now (`hermes send --list` failed); "
                "will retry on the next tick"
            )
            return report
        if platform not in targets:
            report["reason"] = (
                f"channel '{platform}' is not connected on this profile"
                + (f" (detected: {', '.join(targets)})" if targets
                   else " — no delivery platforms are connected yet")
            )
            return report
    ok, reason = timing.can_send(model, force=force)
    report["reason"] = reason
    if not ok:
        return report
    text = delivery.build_ping(dispatch, model["settings"]["dashboard_url"], home)
    # At-most-once: mark the attempt before sending. If this process dies
    # mid-send (gateway restart, a chat closing) the child may still deliver;
    # the next tick then treats the stale marker as sent instead of sending
    # again. Missing one nudge beats pinging the user twice.
    timing.mark_sending(dispatch["id"])
    delivered, detail = delivery.send(
        model["settings"]["channel"], text, home, dispatch["id"]
    )
    if delivered:
        timing.record_sent(dispatch["id"])
        report["sent"] = dispatch["id"]
        snapshot.write_snapshot(journey.build_journey(home))
    elif delivered is None:
        # Outcome unknown (send timed out): the child may still deliver. Keep
        # the marker so it settles as sent (unconfirmed) — never resend.
        report["reason"] = f"delivery outcome unknown, not retrying: {detail}"
    else:
        timing.clear_sending(dispatch["id"])
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
    channel = (s.get("channel") or "").strip()
    known = model.get("gateway_targets", [])
    if (channel and "gateway_targets" in model
            and channel.split(":")[0].strip().lower() not in known):
        lines.append(
            f"Warning: channel '{channel}' is not connected on this profile "
            f"(detected: {', '.join(known) or 'none'}) — pings can't be sent. "
            "Pick a connected channel, or clear it for dashboard-only mode."
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
        stop = _LOOPS.pop(home, None)
        if stop is not None:
            stop.set()
        _LOOP_CTX.pop(home, None)

    ctx.on_unload(_cleanup)
    _start_loop(ctx, home)
