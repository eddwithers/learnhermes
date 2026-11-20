"""Delivery — a short ping to the user's chosen channel, plus a durable outbox.

The ping carries the announcement and a link; the dashboard carries the full
lesson. Channel is any `hermes send` target (telegram, discord:#chan, push,
email, ...). The subprocess is bound to the owning profile explicitly —
never the inherited environment of a multiplexed gateway.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from plugins.plugin_storage import plugin_data_dir


def _hermes_cmd() -> list[str]:
    """Resolve the hermes CLI robustly — launchd/plugin-host PATH can be thin."""
    exe = shutil.which("hermes")
    if exe:
        return [exe]
    fallback = Path.home() / ".local" / "bin" / "hermes"
    if fallback.exists():
        return [str(fallback)]
    return ["hermes"]


def dashboard_link(base_url: str, home: str) -> str:
    """Scope the dashboard URL to the owning profile when it is a named one.

    The multiplex dashboard resolves plugin routes against the default
    profile unless ?profile=<name> is present — a bare link from a named
    profile would show 'Plugin not found'. Query params are parsed, not
    substring-matched (?notprofile=1 must not block scoping).
    """
    home_path = Path(home)
    if home_path.parent.name != "profiles":
        return base_url
    parts = urlsplit(base_url)
    params = parse_qsl(parts.query, keep_blank_values=True)
    if any(key == "profile" for key, _ in params):
        return base_url
    params.append(("profile", home_path.name))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def build_ping(dispatch: dict, dashboard_url: str, home: str) -> str:
    why = " ".join(dispatch["why"].split())
    if len(why) > 160:
        cut = why[:160].rfind(". ")
        why = why[: cut + 1] if cut > 40 else why[:157] + "..."
    return (
        f"LearnHermes — Dispatch {dispatch['n']}: {dispatch['title']}\n"
        f"{why}\n"
        f"Open your journey: {dashboard_link(dashboard_url, home)}"
    )


def _outbox_append(record: dict) -> None:
    p = plugin_data_dir("learnhermes") / "outbox.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def send(channel: str, text: str, home: str, dispatch_id: str) -> tuple[bool, str]:
    """Send via hermes send. Always records to the outbox, send or no send."""
    env = {**os.environ, "HERMES_HOME": str(home)}
    record = {
        "ts": time.time(),
        "dispatch_id": dispatch_id,
        "channel": channel,
        "text": text,
        "delivered": False,
        "detail": "",
    }
    try:
        proc = subprocess.run(
            _hermes_cmd() + ["send", "--to", channel, "--quiet", text],
            env=env, capture_output=True, text=True, timeout=45,
        )
        record["delivered"] = proc.returncode == 0
        record["detail"] = (proc.stderr or proc.stdout or "").strip()[:300]
    except (subprocess.TimeoutExpired, OSError) as exc:
        record["detail"] = str(exc)[:300]
    _outbox_append(record)
    return record["delivered"], record["detail"]


def list_targets(home: str) -> list[str]:
    """Configured delivery platforms, via Hermes's own CLI (no .env reads here).

    `hermes send --list` prints non-indented section headers and indented
    target lines. We only need which platforms exist, deduplicated.
    """
    env = {**os.environ, "HERMES_HOME": str(home)}
    try:
        proc = subprocess.run(
            _hermes_cmd() + ["send", "--list"],
            env=env, capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    platforms: list[str] = []
    for raw in (proc.stdout or "").splitlines():
        if not raw.startswith((" ", "\t")):
            continue
        line = raw.strip()
        if not line:
            continue
        platform = line.split()[0].split(":")[0]
        if platform.isalpha() and platform.islower() and len(platform) < 20:
            if platform not in platforms:
                platforms.append(platform)
    return platforms
