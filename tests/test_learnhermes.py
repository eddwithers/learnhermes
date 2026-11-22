"""Deterministic behaviour tests for LearnHermes — no network, no real home.

Run from the Hermes checkout:
  scripts/run_tests.sh /absolute/path/to/learnhermes/tests -q
"""

import json
import sys
import time
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT.parent))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + plugin data dir."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: h, raising=False)
    import plugins.plugin_storage as ps
    monkeypatch.setattr(ps, "plugin_data_dir", lambda name: h / "plugin-data" / name, raising=False)
    # rebind every module-level import of plugin_data_dir
    import learnhermes.learner as learner
    import learnhermes.delivery as delivery
    import learnhermes.snapshot as snapshot
    monkeypatch.setattr(learner, "plugin_data_dir", lambda name: h / "plugin-data" / name)
    monkeypatch.setattr(delivery, "plugin_data_dir", lambda name: h / "plugin-data" / name)
    monkeypatch.setattr(snapshot, "plugin_data_dir", lambda name: h / "plugin-data" / name)
    return h


@pytest.fixture()
def lh(home):
    import learnhermes
    return learnhermes


def _write_cron(home, n=1):
    cron = home / "cron"
    cron.mkdir(exist_ok=True)
    (cron / "jobs.json").write_text(json.dumps({"jobs": [{"id": f"j{i}"} for i in range(n)]}))


def _write_session_db(home, n=1, age_days=0):
    import sqlite3
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL)")
    now = time.time() - age_days * 86400
    for i in range(n):
        conn.execute("INSERT INTO sessions VALUES (?, ?)", (f"s{i}", now - i))
    conn.commit()
    conn.close()


# --- observer: evidence, not claims -------------------------------------

def test_milestones_empty_home(lh, home):
    results = lh.observer.scan(home, {"tool_counts": {}})
    assert all(ok is False for ok, _ in results.values())


def test_cron_milestone_needs_real_job(lh, home):
    _write_cron(home, 1)
    results = lh.observer.scan(home, {"tool_counts": {}})
    assert results["first_cron"][0] is True
    assert "1 cron job" in results["first_cron"][1]


def test_cron_empty_jobs_not_a_milestone(lh, home):
    _write_cron(home, 0)
    results = lh.observer.scan(home, {"tool_counts": {}})
    assert results["first_cron"][0] is False


def test_chat_milestone_from_sessions(lh, home):
    _write_session_db(home, 2)
    results = lh.observer.scan(home, {"tool_counts": {}})
    assert results["first_chat"][0] is True


def test_tool_milestone_from_observed_counts(lh, home):
    results = lh.observer.scan(home, {"tool_counts": {"terminal": 3}})
    assert results["first_tool"][0] is True
    results = lh.observer.scan(home, {"tool_counts": {"skills_list": 5}})
    assert results["first_tool"][0] is False


def test_memory_milestone_needs_content(lh, home):
    (home / "MEMORY.md").write_text("   \n\t  ")
    assert lh.observer.scan(home, {})["memory"][0] is False
    (home / "MEMORY.md").write_text("Edd. Brief answers.")  # short but real
    assert lh.observer.scan(home, {})["memory"][0] is True


def test_agent_skill_milestone(lh, home):
    skill = home / "skills" / "my-trick"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: my-trick\ncreated_by: agent\n---\nBody")
    assert lh.observer.scan(home, {})["first_skill"][0] is True
    (skill / "SKILL.md").write_text("---\nname: my-trick\nauthor: someone\n---\nBody")
    assert lh.observer.scan(home, {})["first_skill"][0] is False


def test_skill_body_cannot_fake_frontmatter(lh, home):
    skill = home / "skills" / "sneaky"
    skill.mkdir(parents=True)
    # created_by line lives in the BODY, not the frontmatter — must not count
    (skill / "SKILL.md").write_text(
        "---\nname: sneaky\nauthor: human\n---\nExample:\ncreated_by: agent\n"
    )
    assert lh.observer.scan(home, {})["first_skill"][0] is False


def test_second_profile_milestone(lh, home):
    other = home / "profiles" / "work"
    other.mkdir(parents=True)
    (other / "config.yaml").write_text("model: {}")
    assert lh.observer.scan(home, {})["second_profile"][0] is True


def test_named_profile_does_not_count_itself(lh, home):
    # Viewed from inside a named profile, only OTHER profiles count.
    named = home / "profiles" / "work"
    named.mkdir(parents=True)
    (named / "config.yaml").write_text("model: {}")
    assert lh.observer.scan(named, {})["second_profile"][0] is False
    sibling = home / "profiles" / "play"
    sibling.mkdir()
    (sibling / "SOUL.md").write_text("persona")
    assert lh.observer.scan(named, {})["second_profile"][0] is True


# --- learner model -------------------------------------------------------

def test_milestone_marks_dispatch_done(lh, home):
    lh.learner.mutate(lambda m: m["dispatches"].update({"first_cron": {"state": "sent"}}))
    assert lh.learner.mark_milestone("first_cron", "1 job") is True
    model = lh.learner.load()
    assert model["dispatches"]["first_cron"]["state"] == "done"
    assert lh.learner.mark_milestone("first_cron", "1 job") is False  # idempotent


def test_pending_dispatch_blocks_selection(lh, home):
    lh.learner.mutate(lambda m: m["dispatches"].update({"memory": {"state": "sent"}}))
    model = lh.learner.load()
    assert lh.timing.select(lh.timing.load_catalogue(), model) is None


# --- timing rules --------------------------------------------------------

def _model_ready(now=None, **over):
    import copy
    from learnhermes.learner import DEFAULT_MODEL
    now = now if now is not None else time.time()
    m = copy.deepcopy(DEFAULT_MODEL)
    m["settings"]["channel"] = "telegram"
    m["activity"] = {"last_activity": now - 3600, "active": False}
    m.update(over)
    return m


def test_can_send_ok_when_idle(lh):
    now = time.mktime(time.strptime("2026-11-19 12:00", "%Y-%m-%d %H:%M"))
    m = _model_ready(now=now)
    ok, reason = lh.timing.can_send(m, now=now)
    assert ok, reason


def test_outside_send_window_blocks(lh):
    now = time.mktime(time.strptime("2026-11-19 22:00", "%Y-%m-%d %H:%M"))
    ok, reason = lh.timing.can_send(_model_ready(now=now), now=now)
    assert not ok and reason == "outside send window"


def test_active_session_blocks(lh):
    now = time.mktime(time.strptime("2026-11-19 12:00", "%Y-%m-%d %H:%M"))
    m = _model_ready(activity={"last_activity": now, "active": True})
    ok, reason = lh.timing.can_send(m, now=now)
    assert not ok and reason == "session active"


def test_not_idle_blocks(lh):
    now = time.mktime(time.strptime("2026-11-19 12:00", "%Y-%m-%d %H:%M"))
    m = _model_ready(activity={"last_activity": now - 60, "active": False})
    ok, reason = lh.timing.can_send(m, now=now)
    assert not ok and reason == "user not idle yet"


def test_daily_cap_blocks(lh):
    now = time.mktime(time.strptime("2026-11-19 12:00", "%Y-%m-%d %H:%M"))
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    m = _model_ready(sent_today={"date": today, "count": 2})
    ok, reason = lh.timing.can_send(m, now=now)
    assert not ok and reason == "daily cap reached"


def test_pace_gap_blocks(lh):
    now = time.mktime(time.strptime("2026-11-19 12:00", "%Y-%m-%d %H:%M"))
    m = _model_ready(now=now, last_sent_at=now - 3600, sessions_7d=0)  # quiet pace = 24h gap
    ok, reason = lh.timing.can_send(m, now=now)
    assert not ok and reason.startswith("pace gap")


def test_fast_pace_shorter_gap(lh):
    assert lh.timing.pace(_model_ready(), sessions_7d=20) == "fast"
    assert lh.timing.pace(_model_ready(), sessions_7d=5) == "steady"
    assert lh.timing.pace(_model_ready(), sessions_7d=0) == "quiet"
    now = time.mktime(time.strptime("2026-11-19 12:00", "%Y-%m-%d %H:%M"))
    m = _model_ready(now=now, last_sent_at=now - 5 * 3600, sessions_7d=20)
    ok, _ = lh.timing.can_send(m, now=now)
    assert ok  # 5h > 4h fast gap


def test_no_channel_blocks(lh):
    m = _model_ready()
    m["settings"]["channel"] = ""
    ok, reason = lh.timing.can_send(m)
    assert not ok and reason == "no channel configured"


def test_selection_skips_achieved_and_dismissed(lh, home):
    lh.learner.mark_milestone("first_chat", "1 session")
    lh.timing.dismiss("memory")
    model = lh.learner.load()
    nxt = lh.timing.select(lh.timing.load_catalogue(), model)
    assert nxt["id"] == "first_tool"


def test_record_sent_updates_cap_and_state(lh, home):
    now = time.mktime(time.strptime("2026-11-19 12:00", "%Y-%m-%d %H:%M"))
    lh.timing.record_sent("first_tool", now=now)
    m = lh.learner.load()
    assert m["dispatches"]["first_tool"]["state"] == "sent"
    assert m["sent_today"]["count"] == 1
    assert m["last_sent_at"] == now


# --- delivery ------------------------------------------------------------

def test_ping_is_short_and_links_dashboard(lh, home):
    d = lh.timing.load_catalogue()[3]
    text = lh.delivery.build_ping(d, "http://127.0.0.1:9119/learnhermes", str(home))
    assert f"Dispatch {d['n']}" in text
    assert "http://127.0.0.1:9119/learnhermes" in text
    assert len(text) < 400


def test_ping_link_scoped_for_named_profile(lh, home):
    named = home / "profiles" / "work"
    named.mkdir(parents=True)
    d = lh.timing.load_catalogue()[0]
    text = lh.delivery.build_ping(d, "http://host:9119/learnhermes", str(named))
    assert "http://host:9119/learnhermes?profile=work" in text
    # default-profile home: no scoping param
    text2 = lh.delivery.build_ping(d, "http://host:9119/learnhermes", str(home))
    assert "profile=" not in text2
    # already-scoped URL: no duplicate param
    text3 = lh.delivery.build_ping(d, "http://host:9119/learnhermes?profile=work", str(named))
    assert text3.count("profile=") == 1


def test_dashboard_link_parses_query_not_substring(lh, home):
    named = home / "profiles" / "work"
    named.mkdir(parents=True)
    link = lh.delivery.dashboard_link
    # a lookalike param must not block scoping
    assert link("http://h/x?notprofile=1", str(named)) == "http://h/x?notprofile=1&profile=work"
    # existing params preserved
    assert link("http://h/x?foo=bar", str(named)) == "http://h/x?foo=bar&profile=work"
    # real profile param respected, whatever its value
    assert link("http://h/x?profile=other", str(named)) == "http://h/x?profile=other"
    # fragment survives
    assert link("http://h/x#top", str(named)) == "http://h/x?profile=work#top"


def test_learner_recovers_from_wrong_shape_state(lh, home):
    p = lh.learner.model_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[1, 2, 3]")  # parseable JSON, wrong shape
    m = lh.learner.load()  # must not raise
    assert m["settings"]["channel"] == ""
    assert p.with_suffix(".corrupt").exists()
    p.write_text('"just a string"')
    m = lh.learner.load()
    assert isinstance(m, dict) and "milestones" in m


def test_learner_recovers_from_invalid_utf8(lh, home):
    p = lh.learner.model_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\xff\xfe\x00broken")
    m = lh.learner.load()  # must not raise UnicodeDecodeError
    assert isinstance(m, dict) and m["settings"]["channel"] == ""
    assert p.with_suffix(".corrupt").read_bytes() == b"\xff\xfe\x00broken"
    # and the next mutate works on the recovered model
    lh.learner.mutate(lambda mm: mm["settings"].update({"channel": "push"}))
    assert lh.learner.load()["settings"]["channel"] == "push"


def test_outbox_records_even_without_channel(lh, home, monkeypatch):
    calls = []
    monkeypatch.setattr(lh.delivery.subprocess, "run",
                        lambda *a, **k: calls.append(a) or type("P", (), {"returncode": 1, "stdout": "", "stderr": "no target"})())
    ok, detail = lh.delivery.send("", "hello", str(home), "first_tool")
    assert not ok
    outbox = home / "plugin-data" / "learnhermes" / "outbox.jsonl"
    assert outbox.exists()
    rec = json.loads(outbox.read_text().strip())
    assert rec["dispatch_id"] == "first_tool" and rec["delivered"] is False


# --- send window wraparound ----------------------------------------------

def test_send_window_wraps_midnight(lh):
    s = {"send_start": 22, "send_end": 6}
    assert lh.timing.in_send_window(s, 23) is True
    assert lh.timing.in_send_window(s, 3) is True
    assert lh.timing.in_send_window(s, 6) is False
    assert lh.timing.in_send_window(s, 12) is False
    assert lh.timing.in_send_window(s, 22) is True


def test_send_window_equal_bounds_fail_closed(lh):
    s = {"send_start": 9, "send_end": 9}
    assert lh.timing.in_send_window(s, 9) is False
    assert lh.timing.in_send_window(s, 12) is False


def test_can_send_wraparound_window(lh):
    night = time.mktime(time.strptime("2026-11-19 23:30", "%Y-%m-%d %H:%M"))
    m = _model_ready(now=night)
    m["settings"].update({"send_start": 22, "send_end": 6})
    ok, _ = lh.timing.can_send(m, now=night)
    assert ok  # 23:30 is inside an overnight window
    noon = time.mktime(time.strptime("2026-11-19 12:00", "%Y-%m-%d %H:%M"))
    m2 = _model_ready(now=noon)
    m2["settings"].update({"send_start": 22, "send_end": 6})
    ok, reason = lh.timing.can_send(m2, now=noon)
    assert not ok and reason == "outside send window"


def test_settings_migration_renames_quiet_keys(lh, home):
    # A raw v0.1 file: quiet_* keys, no send_* keys.
    p = lh.learner.model_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "version": 1,
        "settings": {"quiet_start": 8, "quiet_end": 21, "channel": "telegram"},
    }))
    m = lh.learner.load()
    assert m["settings"]["send_start"] == 8
    assert m["settings"]["send_end"] == 21
    assert m["settings"]["channel"] == "telegram"
    assert "quiet_start" not in m["settings"]


# --- CLI resolution -------------------------------------------------------

def test_hermes_cmd_prefers_path(lh, monkeypatch):
    monkeypatch.setattr(lh.delivery.shutil, "which", lambda name: "/usr/local/bin/hermes")
    assert lh.delivery._hermes_cmd() == ["/usr/local/bin/hermes"]


def test_hermes_cmd_fallback_local_bin(lh, monkeypatch):
    monkeypatch.setattr(lh.delivery.shutil, "which", lambda name: None)
    monkeypatch.setattr(lh.delivery.Path, "home", lambda: Path("/nonexistent-home"))
    # fallback absent -> bare name (subprocess will raise a clear OSError)
    assert lh.delivery._hermes_cmd() == ["hermes"]


# --- snapshot -------------------------------------------------------------

def test_snapshot_is_self_contained_and_escaped(lh, home):
    j = lh.journey.build_journey(str(home))
    j["current"]["title"] = "<script>alert(1)</script>"
    html_text = lh.snapshot.render_html(j)
    assert "<script>alert(1)</script>" not in html_text
    assert "&lt;script&gt;" in html_text
    assert "<!doctype html>" in html_text
    assert "http" not in html_text.split("<style>")[0]  # no external head assets
    assert "stylesheet" not in html_text  # inline CSS only


def test_snapshot_written_to_data_dir(lh, home):
    j = lh.journey.build_journey(str(home))
    p = lh.snapshot.write_snapshot(j)
    assert p.exists() and p.parent.name == "learnhermes"
    assert "LearnHermes" in p.read_text()


# --- journey assembly -----------------------------------------------------

def test_journey_current_follows_pending(lh, home):
    lh.learner.mutate(lambda m: m["dispatches"].update({"memory": {"state": "sent"}}))
    j = lh.journey.build_journey(str(home))
    assert j["current"]["id"] == "memory"
    assert j["milestones_total"] == 8


def test_journey_marks_done_from_evidence(lh, home):
    _write_cron(home, 2)
    lh.learner.mark_milestone("first_cron", "2 cron jobs")
    j = lh.journey.build_journey(str(home))
    row = next(r for r in j["dispatches"] if r["id"] == "first_cron")
    assert row["state"] == "done"
    assert "2 cron jobs" in row["evidence"]


# --- conversational surface ------------------------------------------------

def test_tool_status(lh, home):
    handler = lh.tools.make_handler(str(home), lh._tick)
    out = json.loads(handler({"action": "status"}))
    assert out["ok"] and out["milestones_total"] == 8
    assert "settings" in out and "journey" in out


def test_status_surfaces_missing_channel_blocker(lh, home):
    handler = lh.tools.make_handler(str(home), lh._tick)
    out = json.loads(handler({"action": "status"}))
    assert "No delivery channel" in out["next_step"]
    assert "configure" in out["next_step"]


def test_status_next_step_with_channel_and_current(lh, home):
    handler = lh.tools.make_handler(str(home), lh._tick)
    lh.learner.mutate(lambda m: m["settings"].update({"channel": "telegram"}))
    lh.learner.mutate(lambda m: m.update({"gateway_targets": ["telegram"]}))
    out = json.loads(handler({"action": "status"}))
    assert "Current dispatch" in out["next_step"]


def test_status_next_step_when_paused(lh, home):
    handler = lh.tools.make_handler(str(home), lh._tick)
    lh.learner.mutate(lambda m: m["settings"].update({"enabled": False}))
    out = json.loads(handler({"action": "status"}))
    assert "paused" in out["next_step"]


def test_status_includes_scoped_dashboard_link(lh, home):
    named = home / "profiles" / "work"
    named.mkdir(parents=True)
    handler = lh.tools.make_handler(str(named), lh._tick)
    out = json.loads(handler({"action": "status"}))
    assert out["dashboard_link"].endswith("?profile=work")


def test_send_now_surfaces_reason_as_error(lh, home, monkeypatch):
    from learnhermes.dashboard import plugin_api
    monkeypatch.setattr(plugin_api, "get_hermes_home", lambda: home)
    monkeypatch.setattr(lh, "_tick", lambda h, force=False: {
        "newly_achieved": [], "sent": None, "reason": "no channel configured"})
    out = plugin_api.send_now()
    assert out["ok"] is False and out["error"] == "no channel configured"
    monkeypatch.setattr(lh, "_tick", lambda h, force=False: {
        "newly_achieved": [], "sent": "memory", "reason": ""})
    out = plugin_api.send_now()
    assert out["ok"] is True and out["error"] is None


def test_tool_configure_validates(lh, home, monkeypatch):
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: ["telegram"])
    handler = lh.tools.make_handler(str(home), lh._tick)
    out = json.loads(handler({"action": "configure", "channel": "telegram"}))
    assert out["ok"] and lh.learner.load()["settings"]["channel"] == "telegram"
    bad = json.loads(handler({"action": "configure", "send_start": 99}))
    assert not bad["ok"]
    empty = json.loads(handler({"action": "configure"}))
    assert not empty["ok"]


def test_clean_detail_strips_update_noise(lh):
    raw = (
        "hermes: source-update completion failed: an update is still running\n"
        "⚠ A previous `hermes update` pulled new code but did not restart running gateways.\n"
        "  Run `hermes update` or `hermes gateway restart`.\n"
        "hermes send: Unknown or unregistered plugin platform: dashboard"
    )
    out = lh.delivery._clean_detail(raw)
    assert "Unknown or unregistered plugin platform: dashboard" in out
    assert "hermes update" not in out.lower()
    assert "gateway restart" not in out.lower()


def test_validate_channel_rejects_invented_target(lh, home, monkeypatch):
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: ["telegram", "discord"])
    ok, msg = lh.delivery.validate_channel("dashboard", str(home))
    assert not ok and "not a connected delivery platform" in msg
    ok, msg = lh.delivery.validate_channel("here", str(home))
    assert not ok
    assert lh.delivery.validate_channel("telegram", str(home))[0]
    assert lh.delivery.validate_channel("discord:#ops", str(home))[0]
    assert lh.delivery.validate_channel("", str(home))[0]  # dashboard-only


def test_validate_channel_no_platforms_connected(lh, home, monkeypatch):
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: [])
    ok, msg = lh.delivery.validate_channel("telegram", str(home))
    assert not ok and "No delivery platform is connected" in msg
    assert lh.delivery.validate_channel("", str(home))[0]


def test_configure_rejects_unconnected_channel(lh, home, monkeypatch):
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: ["telegram"])
    handler = lh.tools.make_handler(str(home), lh._tick)
    out = json.loads(handler({"action": "configure", "channel": "dashboard"}))
    assert not out["ok"]
    assert lh.learner.load()["settings"]["channel"] == ""  # unchanged


def test_tool_dismiss_requires_open(lh, home):
    handler = lh.tools.make_handler(str(home), lh._tick)
    out = json.loads(handler({"action": "dismiss"}))
    assert not out["ok"]


def test_tool_unknown_action(lh, home):
    handler = lh.tools.make_handler(str(home), lh._tick)
    out = json.loads(handler({"action": "explode"}))
    assert not out["ok"]


def test_welcome_fires_once(lh, home, monkeypatch):
    monkeypatch.setattr(lh, "get_hermes_home", lambda: home)
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: ["telegram"])
    first = lh._on_pre_llm_call("s1", "hi", True)
    assert first and "LearnHermes" in first["context"]
    assert "telegram" in first["context"]
    assert lh._on_pre_llm_call("s1", "hi again", False) is None
    assert lh.learner.load()["welcomed"] is True


def test_welcome_skipped_if_channel_already_set(lh, home, monkeypatch):
    monkeypatch.setattr(lh, "get_hermes_home", lambda: home)
    lh.learner.mutate(lambda m: m["settings"].update({"channel": "push"}))
    assert lh._on_pre_llm_call("s1", "hi", True) is None
    assert lh.learner.load()["welcomed"] is True


def test_welcome_never_consumed_by_automation(lh, home, monkeypatch):
    monkeypatch.setattr(lh, "get_hermes_home", lambda: home)
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: ["telegram"])
    for platform in ("cron", "subagent", "webhook", "api"):
        assert lh._on_pre_llm_call("s1", "job output", True, platform=platform) is None
    assert lh.learner.load().get("welcomed") is not True
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_123")
    assert lh._on_pre_llm_call("s1", "worker turn", True, platform="cli") is None
    assert lh.learner.load().get("welcomed") is not True
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    # the user's real first chat still gets it
    assert lh._on_pre_llm_call("s1", "hi", True, platform="cli") is not None


# --- v0.1.3: timing-loop lifecycle ------------------------------------------
# register() never runs with an asyncio loop on its own thread: in-process it
# runs on a plugin-load worker thread, in the isolated host on the channel
# request thread. The old get_running_loop() guard silently skipped arming in
# both, so after a gateway restart a quiet profile (the exact dispatch
# scenario) never ticked again until someone chatted. The loop must arm at
# register time in every mode, and stop on unload.

class _FakeCtx:
    """Minimal plugin context. Default spawn_task is host-like: it schedules
    onto its own loop from ANY thread (run_coroutine_threadsafe semantics)."""

    def __init__(self, spawn=None):
        self.spawned = []
        self.unload_callbacks = []
        self._spawn = spawn

    def spawn_task(self, coro, *, name=None):
        if self._spawn is not None:
            return self._spawn(coro, name)
        import asyncio
        import threading
        t = threading.Thread(target=asyncio.run, args=(coro,), daemon=True)
        t.start()
        self.spawned.append(name)
        return t

    def register_tool(self, **kw):
        pass

    def register_hook(self, *a, **kw):
        pass

    def register_command(self, *a, **kw):
        pass

    def register_skill(self, *a, **kw):
        pass

    def on_unload(self, cb):
        self.unload_callbacks.append(cb)


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_loop_arms_via_spawn_task_without_running_loop(lh, home, monkeypatch):
    calls = []
    monkeypatch.setattr(lh, "_tick", lambda h, force=False: calls.append(h) or {})
    monkeypatch.setattr(lh, "_TICK_SECONDS", 0.05)
    monkeypatch.setattr(lh, "get_hermes_home", lambda: home)
    ctx = _FakeCtx()
    lh.register(ctx)
    try:
        assert ctx.spawned, "timing loop was not spawned at register time"
        assert _wait_for(lambda: len(calls) >= 1), "loop spawned but never ticked"
    finally:
        for cb in ctx.unload_callbacks:
            cb()
    assert str(home) not in lh._LOOPS


def test_loop_falls_back_to_thread_when_spawn_unavailable(lh, home, monkeypatch):
    # In-process load with no running loop: spawn_task raises RuntimeError.
    # The loop must still arm via the daemon-thread fallback.
    calls = []
    monkeypatch.setattr(lh, "_tick", lambda h, force=False: calls.append(h) or {})
    monkeypatch.setattr(lh, "_TICK_SECONDS", 0.05)

    def refuse(coro, name):
        raise RuntimeError("no running event loop")

    ctx = _FakeCtx(spawn=refuse)
    lh._start_loop(ctx, str(home))
    stop = None
    try:
        assert _wait_for(lambda: len(calls) >= 1), "thread fallback never ticked"
        stop = lh._LOOPS.get(str(home))
        assert stop is not None
        # double-start is a no-op while the loop is live
        lh._start_loop(ctx, str(home))
        assert lh._LOOPS.get(str(home)) is stop
    finally:
        stop = lh._LOOPS.pop(str(home), None)
        if stop is not None:
            stop.set()


def test_first_tick_waits_a_full_interval(lh, home, monkeypatch):
    # A short-lived command must exit before its first tick. The loop waits
    # an interval BEFORE ticking, never ticks on arming.
    calls = []
    monkeypatch.setattr(lh, "_tick", lambda h, force=False: calls.append(h) or {})
    monkeypatch.setattr(lh, "_TICK_SECONDS", 0.4)

    def refuse(coro, name):
        raise RuntimeError("no running event loop")

    lh._start_loop(_FakeCtx(spawn=refuse), str(home))
    try:
        time.sleep(0.15)
        assert calls == [], "ticked immediately on arming"
        assert _wait_for(lambda: len(calls) >= 1, timeout=2.0)
    finally:
        stop = lh._LOOPS.pop(str(home), None)
        if stop is not None:
            stop.set()


def test_host_loop_first_tick_waits_too(lh, home, monkeypatch):
    # Same contract on the spawn_task (plugin host) path. Generous margins:
    # interval 1.0s, check at 0.3s, so a loaded machine can't flake it.
    import asyncio
    calls = []
    monkeypatch.setattr(lh, "_tick", lambda h, force=False: calls.append(h) or {})
    monkeypatch.setattr(lh, "_TICK_SECONDS", 1.0)
    stop = __import__("threading").Event()

    async def run():
        task = asyncio.ensure_future(lh._tick_loop(str(home), stop))
        await asyncio.sleep(0.3)
        early = list(calls)
        for _ in range(60):  # up to 6s for the first tick
            if calls:
                break
            await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        return early

    early = asyncio.run(run())
    assert early == [] and len(calls) >= 1


def test_host_loop_wait_does_not_park_an_executor_thread(lh, home, monkeypatch):
    # The between-ticks wait must not sit in the default executor: interpreter
    # exit joins those threads, so a parked one delays shutdown by the whole
    # interval (seen with a 20s interval: exit took 20.9s).
    import asyncio
    import threading
    monkeypatch.setattr(lh, "_tick", lambda h, force=False: {})
    monkeypatch.setattr(lh, "_TICK_SECONDS", 30)
    stop = threading.Event()

    async def run():
        task = asyncio.ensure_future(lh._tick_loop(str(home), stop))
        await asyncio.sleep(0.3)
        parked = [t.name for t in threading.enumerate() if t.name.startswith("asyncio_")]
        stop.set()
        await asyncio.wait_for(task, timeout=5)  # stop is honoured promptly
        return parked

    assert asyncio.run(run()) == []


def test_unload_stops_the_loop(lh, home, monkeypatch):
    calls = []
    monkeypatch.setattr(lh, "_tick", lambda h, force=False: calls.append(h) or {})
    monkeypatch.setattr(lh, "_TICK_SECONDS", 0.05)
    monkeypatch.setattr(lh, "get_hermes_home", lambda: home)
    ctx = _FakeCtx()
    lh.register(ctx)
    try:
        assert _wait_for(lambda: len(calls) >= 1)
    finally:
        for cb in ctx.unload_callbacks:
            cb()
    assert str(home) not in lh._LOOPS
    n = len(calls)
    time.sleep(0.15)  # ~3 tick periods
    assert len(calls) <= n + 1, "loop kept ticking after unload"


# --- v0.1.3: saved-but-unconnected channel must surface, not stall ----------
# A channel saved by older code (or left behind after a platform disconnect)
# must not silently fail every tick: the tick reports the blocker, status
# guides the user, and BOTH write paths (tool + dashboard) validate.

def _ready_model(lh, channel, targets):
    now = time.time()
    lh.learner.mutate(lambda m: m.update({
        "gateway_targets": targets,
        "activity": {"last_activity": now - 3600, "active": False},
    }))
    lh.learner.mutate(lambda m: m["settings"].update({"channel": channel}))


def test_tick_skips_unconnected_channel(lh, home, monkeypatch):
    _ready_model(lh, "email", ["telegram"])
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: ["telegram"])
    sent = []
    monkeypatch.setattr(lh.delivery, "send", lambda *a, **k: sent.append(a) or (True, ""))
    report = lh._tick(str(home))
    assert report["sent"] is None
    assert "not connected" in report["reason"] and "email" in report["reason"]
    assert not sent
    assert not (home / "plugin-data" / "learnhermes" / "outbox.jsonl").exists()


def test_tick_force_surfaces_unconnected_channel(lh, home, monkeypatch):
    _ready_model(lh, "email", ["telegram"])
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: ["telegram"])
    sent = []
    monkeypatch.setattr(lh.delivery, "send", lambda *a, **k: sent.append(a) or (True, ""))
    report = lh._tick(str(home), force=True)
    assert report["sent"] is None and "not connected" in report["reason"]
    assert not sent


def test_tick_unconnected_no_platforms_reason(lh, home, monkeypatch):
    _ready_model(lh, "telegram", [])
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: [])
    report = lh._tick(str(home))
    assert report["sent"] is None
    assert "no delivery platforms" in report["reason"]


def test_tick_connected_channel_still_delivers(lh, home, monkeypatch):
    _ready_model(lh, "telegram", ["telegram"])
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: ["telegram"])
    sent = []
    monkeypatch.setattr(lh.delivery, "send",
                        lambda channel, text, h, did: sent.append((channel, did)) or (True, ""))
    report = lh._tick(str(home), force=True)
    assert report["sent"] is not None and sent
    assert lh.learner.load()["dispatches"][report["sent"]]["state"] == "sent"


def test_status_surfaces_unconnected_channel(lh, home):
    _ready_model(lh, "email", ["telegram"])
    handler = lh.tools.make_handler(str(home), lh._tick)
    out = json.loads(handler({"action": "status"}))
    assert "not connected" in out["next_step"]
    assert "configure" in out["next_step"]


def test_dashboard_settings_validates_channel(lh, home, monkeypatch):
    from learnhermes.dashboard import plugin_api
    monkeypatch.setattr(plugin_api, "get_hermes_home", lambda: home)
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: ["telegram"])
    out = plugin_api.settings({"channel": "email"})
    assert out["ok"] is False and "not a connected delivery platform" in out["error"]
    assert lh.learner.load()["settings"]["channel"] == ""  # unchanged
    ok = plugin_api.settings({"channel": "telegram"})
    assert ok["ok"] is True
    assert lh.learner.load()["settings"]["channel"] == "telegram"
    cleared = plugin_api.settings({"channel": ""})
    assert cleared["ok"] is True  # dashboard-only stays allowed


def test_send_uses_json_and_records_the_real_error(lh, home, monkeypatch):
    # --quiet suppressed the CLI's error text (that was the 'silent exit 1').
    # send() must ask for --json and record the actual reason.
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        seen["env"] = k.get("env", {})
        out = json.dumps({"error": "Platform 'email' is not configured.",
                          "warnings": ["no EMAIL_ADDRESS"]})
        return type("P", (), {"returncode": 1, "stdout": out, "stderr": ""})()

    monkeypatch.setattr(lh.delivery.subprocess, "run", fake_run)
    ok, detail = lh.delivery.send("email", "hello", str(home), "memory")
    assert not ok
    assert "--json" in seen["cmd"] and "--quiet" not in seen["cmd"]
    assert "Platform 'email' is not configured." in detail and "no EMAIL_ADDRESS" in detail
    assert seen["env"].get(lh.delivery.SUBPROCESS_ENV) == "1"
    rec = json.loads((home / "plugin-data" / "learnhermes" / "outbox.jsonl").read_text().strip())
    assert rec["detail"] == detail


def test_send_truly_silent_failure_is_honest(lh, home, monkeypatch):
    monkeypatch.setattr(lh.delivery.subprocess, "run",
                        lambda *a, **k: type("P", (), {"returncode": 1, "stdout": "", "stderr": ""})())
    ok, detail = lh.delivery.send("email", "hello", str(home), "memory")
    assert not ok and detail == "hermes send exited 1 with no output"  # no guessed cause


# --- v0.1.3: list_targets reads JSON; a failed listing is "unknown" ---------

def _proc(rc, out):
    return type("P", (), {"returncode": rc, "stdout": out, "stderr": ""})()


def test_list_targets_parses_json_including_empty_and_underscored(lh, home, monkeypatch):
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"], seen["env"] = cmd, k.get("env", {})
        return _proc(0, json.dumps({"platforms": {
            "telegram": [{"id": "1"}],
            "whatsapp_cloud": [],        # underscore — old scraper dropped it
            "simplex": [],               # configured, no discovered channels — still a target
        }}))

    monkeypatch.setattr(lh.delivery.subprocess, "run", fake_run)
    assert lh.delivery.list_targets(str(home)) == ["telegram", "whatsapp_cloud", "simplex"]
    assert seen["cmd"][-2:] == ["--list", "--json"]
    assert seen["env"].get(lh.delivery.SUBPROCESS_ENV) == "1"


def test_list_targets_none_on_failure_not_empty(lh, home, monkeypatch):
    import subprocess as sp
    cases = [
        lambda *a, **k: _proc(1, ""),                                   # CLI error
        lambda *a, **k: _proc(0, "not json"),                           # unparseable
        lambda *a, **k: _proc(0, json.dumps({"platforms": ["x"]})),     # wrong shape
    ]
    for fake in cases:
        monkeypatch.setattr(lh.delivery.subprocess, "run", fake)
        assert lh.delivery.list_targets(str(home)) is None

    def timeout(*a, **k):
        raise sp.TimeoutExpired(cmd="hermes", timeout=30)

    monkeypatch.setattr(lh.delivery.subprocess, "run", timeout)
    assert lh.delivery.list_targets(str(home)) is None
    monkeypatch.setattr(lh.delivery.subprocess, "run", lambda *a, **k: _proc(0, '{"platforms": {}}'))
    assert lh.delivery.list_targets(str(home)) == []  # genuinely none connected


def test_tick_listing_failure_keeps_known_targets_and_does_not_block_as_none(lh, home, monkeypatch):
    _ready_model(lh, "telegram", ["telegram"])
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: None)
    sent = []
    monkeypatch.setattr(lh.delivery, "send", lambda *a, **k: sent.append(a) or (True, ""))
    report = lh._tick(str(home), force=True)
    assert report["sent"] is None and not sent
    assert "couldn't list delivery platforms" in report["reason"]
    assert "not connected" not in report["reason"]
    assert lh.learner.load()["gateway_targets"] == ["telegram"]  # last known kept


def test_validate_channel_listing_failure_is_not_none_connected(lh, home, monkeypatch):
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: None)
    ok, msg = lh.delivery.validate_channel("telegram", str(home))
    assert not ok and "Couldn't list" in msg and "No delivery platform is connected" not in msg


def test_dispatches_status_warns_on_unconnected_channel(lh, home):
    _ready_model(lh, "email", ["telegram"])
    out = lh._handle_dispatches(None, str(home), "")
    assert "Warning: channel 'email' is not connected" in out
    _ready_model(lh, "telegram", ["telegram"])
    assert "Warning" not in lh._handle_dispatches(None, str(home), "")


# --- v0.1.3: no self-spawning process chain --------------------------------

def test_subprocess_marker_blocks_arming(lh, home, monkeypatch):
    monkeypatch.setenv(lh.delivery.SUBPROCESS_ENV, "1")
    ctx = _FakeCtx()
    lh._start_loop(ctx, str(home))
    assert not ctx.spawned and str(home) not in lh._LOOPS


def test_start_loop_is_race_free(lh, home, monkeypatch):
    # register() and a session-start hook on another thread must not double-arm.
    import threading
    monkeypatch.setattr(lh, "_tick", lambda h, force=False: {})
    monkeypatch.setattr(lh, "_TICK_SECONDS", 0.05)
    ctx = _FakeCtx()
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        lh._start_loop(ctx, str(home))

    threads = [threading.Thread(target=go) for _ in range(8)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(ctx.spawned) == 1
    finally:
        stop = lh._LOOPS.pop(str(home), None)
        if stop is not None:
            stop.set()


def test_fallback_thread_keeps_profile_scope(lh, home, monkeypatch):
    # The profile-home override is a contextvar; a bare Thread starts with an
    # empty context and would tick against the process home instead.
    import contextvars
    scope = contextvars.ContextVar("lh_test_scope", default="process-home")
    seen = []
    monkeypatch.setattr(lh, "_tick", lambda h, force=False: seen.append(scope.get()) or {})
    monkeypatch.setattr(lh, "_TICK_SECONDS", 0.05)

    def refuse(coro, name):
        raise RuntimeError("no running event loop")

    token = scope.set("profile-pb")
    try:
        lh._start_loop(_FakeCtx(spawn=refuse), str(home))
    finally:
        scope.reset(token)
    try:
        assert _wait_for(lambda: len(seen) >= 1)
        assert seen[0] == "profile-pb"
    finally:
        stop = lh._LOOPS.pop(str(home), None)
        if stop is not None:
            stop.set()


def _run_register_child(home, tmp_path, *, marker: bool, linger: float = 0.0):
    """Run register() in a real separate python process (not the real hermes
    CLI or plugin loader) and report what it armed and how long exit took.
    `linger` keeps the child alive after register(), like a real command
    doing its work (real `hermes` commands take 1-2s)."""
    import os
    import subprocess
    import learnhermes.delivery as delivery
    script = tmp_path / "child.py"
    script.write_text(
        "import sys, threading, time\n"
        f"sys.path[:0] = [{str(PLUGIN_ROOT.parent)!r}, {str(Path(__import__('hermes_constants').__file__).parent)!r}]\n"
        "import learnhermes as lh\n"
        "class Ctx:\n"
        "    def __getattr__(self, n):\n"
        "        return lambda *a, **k: None\n"
        "    def spawn_task(self, coro, name=None):\n"
        "        raise RuntimeError('no running event loop')\n"
        "lh.register(Ctx())\n"
        "print('armed' if lh._LOOPS else 'not-armed')\n"
        "print(len([t for t in threading.enumerate() if t.name.startswith('learnhermes-timing')]), flush=True)\n"
        f"time.sleep({linger!r})\n"
    )
    env = {**os.environ, "HERMES_HOME": str(home)}
    env.pop(delivery.SUBPROCESS_ENV, None)
    if marker:
        env[delivery.SUBPROCESS_ENV] = "1"
    started = time.time()
    out = subprocess.run([sys.executable, str(script)], env=env,
                         capture_output=True, text=True, timeout=60)
    return out, time.time() - started


def test_marked_child_process_does_not_arm(home, tmp_path):
    # A real child process carrying the marker runs register() but must not arm.
    out, _ = _run_register_child(home, tmp_path, marker=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["not-armed", "0"]
    assert not (home / "plugin-data" / "learnhermes" / "learner.json").exists()


def test_short_lived_unmarked_process_exits_promptly_and_never_ticks(home, tmp_path):
    # An ordinary short-lived command (no marker) arms, but must never tick
    # while it does its work, and must exit as soon as it's done (no hang on
    # interpreter shutdown): no scan, no send, no state written. The child
    # lingers 3s — longer than a real command — so an immediate first tick
    # would have time to write learner.json.
    out, elapsed = _run_register_child(home, tmp_path, marker=False, linger=3.0)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["armed", "1"]
    assert elapsed < 15, f"process took {elapsed:.1f}s to exit"
    data = home / "plugin-data" / "learnhermes"
    assert not (data / "learner.json").exists(), "a short-lived process ticked"
    assert not (data / "outbox.jsonl").exists()


# --- v0.1.3: at-most-once delivery across process death ---------------------

def test_interrupted_send_is_settled_as_sent_not_resent(lh, home, monkeypatch):
    # A process died mid-send: the marker remains. The child may have
    # delivered, so the next tick must not send the same dispatch again.
    _ready_model(lh, "telegram", ["telegram"])
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: ["telegram"])
    sent = []
    monkeypatch.setattr(lh.delivery, "send", lambda *a, **k: sent.append(a[3]) or (True, ""))
    first = lh.timing.select(lh.timing.load_catalogue(), lh.learner.load())
    lh.timing.mark_sending(first["id"], now=time.time() - lh.timing.SENDING_STALE_SECONDS - 5)
    report = lh._tick(str(home), force=True)
    assert first["id"] not in sent
    d = lh.learner.load()["dispatches"][first["id"]]
    assert d["state"] == "sent" and d.get("unconfirmed") is True
    assert report["sent"] is None  # an open (sent) dispatch blocks the next one


def test_live_send_marker_blocks_a_concurrent_send(lh, home, monkeypatch):
    # A fresh marker (another process is mid-send) is NOT stale: hold off.
    _ready_model(lh, "telegram", ["telegram"])
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: ["telegram"])
    sent = []
    monkeypatch.setattr(lh.delivery, "send", lambda *a, **k: sent.append(a[3]) or (True, ""))
    first = lh.timing.select(lh.timing.load_catalogue(), lh.learner.load())
    lh.timing.mark_sending(first["id"])
    report = lh._tick(str(home), force=True)
    assert sent == [] and report["sent"] is None
    assert lh.learner.load()["dispatches"][first["id"]]["state"] == "sending"


def test_failed_send_clears_the_marker(lh, home, monkeypatch):
    # The attempt is marked "sending" while the send runs, and a failure
    # clears it so the dispatch stays retryable rather than stuck.
    _ready_model(lh, "telegram", ["telegram"])
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: ["telegram"])
    during = {}

    def failing_send(channel, text, h, did):
        during[did] = lh.learner.load()["dispatches"].get(did, {}).get("state")
        return False, "bot blocked"

    monkeypatch.setattr(lh.delivery, "send", failing_send)
    report = lh._tick(str(home), force=True)
    assert report["sent"] is None and "bot blocked" in report["reason"]
    assert list(during.values()) == ["sending"]  # marked before the send
    assert lh.learner.load()["dispatches"] == {}  # cleared after the failure


def test_send_timeout_is_unknown_and_never_resent(lh, home, monkeypatch):
    # A send that times out may still deliver (the child keeps running). The
    # outcome is unknown: the marker must stay, so it settles as sent instead
    # of being retried into a duplicate.
    import subprocess as sp

    def slow(*a, **k):
        raise sp.TimeoutExpired(cmd="hermes", timeout=45)

    monkeypatch.setattr(lh.delivery.subprocess, "run", slow)
    ok, detail = lh.delivery.send("telegram", "hi", str(home), "memory")
    assert ok is None and "may still have been delivered" in detail

    _ready_model(lh, "telegram", ["telegram"])
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: ["telegram"])
    monkeypatch.setattr(lh.delivery, "send", lambda *a, **k: (None, "timed out"))
    report = lh._tick(str(home), force=True)
    did = next(iter(lh.learner.load()["dispatches"]))
    assert "outcome unknown, not retrying" in report["reason"]
    assert lh.learner.load()["dispatches"][did]["state"] == "sending"  # kept, not cleared

    sent = []
    monkeypatch.setattr(lh.delivery, "send", lambda *a, **k: sent.append(a[3]) or (True, ""))
    later = time.time() + lh.timing.SENDING_STALE_SECONDS + 5
    monkeypatch.setattr(lh.timing.time, "time", lambda: later)
    lh._tick(str(home), force=True)
    assert did not in sent  # never resent
    assert lh.learner.load()["dispatches"][did]["state"] == "sent"


def test_settled_send_counts_toward_daily_cap(lh, home):
    now = time.time()
    lh.timing.mark_sending("memory", now=now - lh.timing.SENDING_STALE_SECONDS - 5)
    lh.timing.settle_stale_sends(now=now)
    m = lh.learner.load()
    assert m["sent_today"]["count"] == 1
    assert m["dispatches"]["memory"]["unconfirmed"] is True


def test_never_listed_targets_say_not_checked_not_none(lh, home):
    # Before any successful listing, no surface may say "none detected".
    j = lh.journey.build_journey(str(home))
    assert j["targets_known"] is False
    html = lh.snapshot.render_html(j)
    assert "none detected" not in html and "not checked yet" in html
    handler = lh.tools.make_handler(str(home), lh._tick)
    out = json.loads(handler({"action": "status"}))
    assert out["detected_platforms_known"] is False
    assert "none detected" not in out["next_step"] and "not checked yet" in out["next_step"]
    lh.learner.mutate(lambda m: m.update({"gateway_targets": []}))  # listed: genuinely none
    out = json.loads(handler({"action": "status"}))
    assert out["detected_platforms_known"] is True and "none detected" in out["next_step"]


def test_never_listed_targets_are_unknown_not_unconnected(lh, home):
    # No successful listing yet: surfaces must not claim "not connected".
    lh.learner.mutate(lambda m: m["settings"].update({"channel": "telegram"}))
    assert "gateway_targets" not in lh.learner.load()
    j = lh.journey.build_journey(str(home))
    assert j["targets_known"] is False
    assert "not connected" not in lh.snapshot.render_html(j)
    assert "Warning" not in lh._handle_dispatches(None, str(home), "")
    handler = lh.tools.make_handler(str(home), lh._tick)
    out = json.loads(handler({"action": "status"}))
    assert "not connected" not in out["next_step"]


def test_tick_is_serialised_across_processes(lh, home, monkeypatch):
    # Two processes ticking the same profile at once must not both deliver.
    # A real second process holds the tick lock; this tick must skip, not send.
    import os
    import subprocess
    sent = []
    _ready_model(lh, "telegram", ["telegram"])
    monkeypatch.setattr(lh.delivery, "list_targets", lambda h: ["telegram"])
    monkeypatch.setattr(lh.delivery, "send", lambda *a, **k: sent.append(a) or (True, ""))
    lock = home / "plugin-data" / "learnhermes" / "tick.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, sys, time\n"
         f"fh = open({str(lock)!r}, 'a+')\n"
         "fcntl.flock(fh, fcntl.LOCK_EX)\n"
         "print('held', flush=True)\n"
         "time.sleep(30)\n"],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        report = lh._tick(str(home), force=True)
        assert report["sent"] is None and not sent
        assert "another LearnHermes tick is running" in report["reason"]
    finally:
        holder.kill()
        holder.wait()
    report = lh._tick(str(home), force=True)  # lock released -> delivers
    assert report["sent"] is not None and len(sent) == 1


def test_child_env_never_carries_another_profiles_credentials(lh, home, monkeypatch):
    # Inside a multiplexed gateway os.environ holds the LAUNCH profile's
    # platform tokens. A child `hermes` for another profile must not see them,
    # or it lists — and sends through — the wrong profile's bots.
    for k in ("TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "OPENROUTER_API_KEY",
              "NOUS_API_KEY", "EMAIL_PASSWORD", "BWS_ACCESS_TOKEN"):
        monkeypatch.setenv(k, "launch-profile-secret")
    monkeypatch.setenv("HERMES_HOME", "/somewhere/launch-profile")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    env = lh.delivery._child_env(str(home))
    assert "launch-profile-secret" not in env.values()
    assert env["HERMES_HOME"] == str(home)
    assert env[lh.delivery.SUBPROCESS_ENV] == "1"
    assert env["PATH"] == "/usr/bin:/bin" and env["HTTPS_PROXY"] == "http://proxy:3128"


def test_list_and_send_use_the_scrubbed_env(lh, home, monkeypatch):
    # Both spawn sites, not just the helper.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "launch-profile-secret")
    envs = []

    def fake_run(cmd, **k):
        envs.append(k["env"])
        return _proc(0, json.dumps({"platforms": {}}) if "--list" in cmd else json.dumps({"success": True}))

    monkeypatch.setattr(lh.delivery.subprocess, "run", fake_run)
    lh.delivery.list_targets(str(home))
    lh.delivery.send("telegram", "hi", str(home), "memory")
    assert len(envs) == 2
    assert all("TELEGRAM_BOT_TOKEN" not in e for e in envs)


def test_snapshot_warns_on_unconnected_channel(lh, home):
    _ready_model(lh, "email", ["telegram"])
    j = lh.journey.build_journey(str(home))
    assert "not connected" in lh.snapshot.render_html(j)
    _ready_model(lh, "telegram", ["telegram"])
    j = lh.journey.build_journey(str(home))
    assert "not connected" not in lh.snapshot.render_html(j)
    _ready_model(lh, "", ["telegram"])
    j = lh.journey.build_journey(str(home))
    assert "not connected" not in lh.snapshot.render_html(j)
