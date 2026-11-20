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
    (home / "MEMORY.md").write_text("x")
    assert lh.observer.scan(home, {})["memory"][0] is False
    (home / "MEMORY.md").write_text("Edd prefers brief answers. " * 5)
    assert lh.observer.scan(home, {})["memory"][0] is True


def test_agent_skill_milestone(lh, home):
    skill = home / "skills" / "my-trick"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: my-trick\ncreated_by: agent\n---\nBody")
    assert lh.observer.scan(home, {})["first_skill"][0] is True
    (skill / "SKILL.md").write_text("---\nname: my-trick\nauthor: someone\n---\nBody")
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


def test_tool_configure_validates(lh, home):
    handler = lh.tools.make_handler(str(home), lh._tick)
    out = json.loads(handler({"action": "configure", "channel": "telegram"}))
    assert out["ok"] and lh.learner.load()["settings"]["channel"] == "telegram"
    bad = json.loads(handler({"action": "configure", "send_start": 99}))
    assert not bad["ok"]
    empty = json.loads(handler({"action": "configure"}))
    assert not empty["ok"]


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
