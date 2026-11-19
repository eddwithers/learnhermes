"""Observer — deterministic reads of real Hermes state. No LLM, no network.

Every milestone is verified from an artifact that exists, never from what
the user or the agent claims. All paths are profile-scoped via the home
passed in (captured at register time, never os.environ at call time).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path


def _session_stats(home: Path) -> tuple[int, float]:
    """(session count, most recent session start ts) from state.db, read-only."""
    db = home / "state.db"
    if not db.exists():
        return 0, 0.0
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute(
                "SELECT COUNT(*), MAX(started_at) FROM sessions"
            ).fetchone()
            return int(row[0] or 0), float(row[1] or 0.0)
        finally:
            conn.close()
    except sqlite3.Error:
        return 0, 0.0


def _cron_job_count(home: Path) -> int:
    p = home / "cron" / "jobs.json"
    if not p.exists():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    return len([j for j in jobs if isinstance(j, dict)])


def _agent_skill_count(home: Path) -> int:
    """Skills the agent wrote (created_by: agent frontmatter), depth <= 2."""
    root = home / "skills"
    if not root.is_dir():
        return 0
    count = 0
    for md in list(root.glob("*/SKILL.md")) + list(root.glob("*/*/SKILL.md")):
        try:
            head = md.read_text(encoding="utf-8", errors="replace")[:2000]
        except OSError:
            continue
        if re.search(r"^created_by:\s*['\"]?agent", head, re.MULTILINE):
            count += 1
    return count


def _memory_written(home: Path) -> bool:
    for name in ("MEMORY.md", "USER.md"):
        p = home / name
        if p.exists() and p.stat().st_size > 50:
            return True
    return False


def _named_profiles(home: Path) -> int:
    # Named profiles live in <root>/profiles/; a profile home is one of them.
    # Count profiles OTHER than the current one: from the default root that
    # is every named profile; from a named profile it excludes itself.
    is_named = home.parent.name == "profiles"
    profiles_root = home.parent if is_named else home / "profiles"
    if not profiles_root.is_dir():
        return 0
    markers = ("config.yaml", ".env", "SOUL.md", "state.db", "auth.json", "profile.yaml")
    n = 0
    for child in profiles_root.iterdir():
        if is_named and child.name == home.name:
            continue
        if child.is_dir() and not child.name.startswith(".") and any(
            (child / m).exists() for m in markers
        ):
            n += 1
    return n


def scan(home: Path, model: dict) -> dict:
    """Verify every milestone against real state. Returns {id: (ok, evidence)}.

    Tool-use milestones come from the observed counters in the model;
    artifact milestones come from disk.
    """
    home = Path(home)
    sessions, last_session = _session_stats(home)
    cron_n = _cron_job_count(home)
    skills_n = _agent_skill_count(home)
    profiles_n = _named_profiles(home)
    tools = model.get("tool_counts", {})
    real_work = sum(
        tools.get(t, 0)
        for t in ("terminal", "read_file", "write_file", "search_files", "web_search", "web_extract", "patch")
    )
    results = {
        "first_chat": (sessions >= 1, f"{sessions} session(s) in state.db"),
        "memory": (_memory_written(home), "MEMORY.md/USER.md has content"),
        "first_tool": (real_work >= 1, f"{real_work} real-work tool call(s) observed"),
        "first_cron": (cron_n >= 1, f"{cron_n} cron job(s) in cron/jobs.json"),
        "first_skill": (skills_n >= 1, f"{skills_n} agent-written skill(s) on disk"),
        "gateway": (bool(model.get("gateway_targets")), "configured send targets: " + ", ".join(model.get("gateway_targets", [])[:4])),
        "delegation": (tools.get("delegate_task", 0) >= 1, "delegate_task observed"),
        "second_profile": (profiles_n >= 1, f"{profiles_n} named profile(s)"),
    }
    return results


def session_count_7d(home: Path) -> int:
    db = Path(home) / "state.db"
    if not db.exists():
        return 0
    cutoff = time.time() - 7 * 86400
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE started_at > ?", (cutoff,)
            ).fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
