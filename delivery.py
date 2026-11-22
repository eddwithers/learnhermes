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


# Hermes CLI infrastructure warnings that are NOT delivery errors. They must
# never be surfaced as the reason a ping failed — doing so sends the agent
# chasing gateway restarts / updates instead of fixing the channel.
_INFRA_NOISE = (
    "hermes update",
    "update is still running",
    "did not restart running gateways",
    "pre-update modules",
    "source-update completion",
    "gateway restart",
    "relaunch hermes",
    "previous dependencies",
    "completing source-update",
    "preparing dependencies",
)


def _clean_detail(raw: str) -> str:
    lines = [
        ln for ln in (raw or "").splitlines()
        if ln.strip() and not any(n in ln.lower() for n in _INFRA_NOISE)
    ]
    return "\n".join(lines).strip()[:300]


# Marks every `hermes` subprocess LearnHermes spawns. That CLI discovers and
# loads plugins, including this one; a child that armed its own timing loop
# would tick, spawn another `hermes send --list`, and so on — a
# self-perpetuating process chain. register() refuses to arm when this is set.
SUBPROCESS_ENV = "LEARNHERMES_SUBPROCESS"

# The ONLY parent variables a child `hermes` inherits. Everything else —
# above all platform tokens — must come from the target profile's own .env
# and config, which `hermes send` loads itself from HERMES_HOME. A
# multiplexed gateway's environment carries the LAUNCH profile's credentials
# (TELEGRAM_BOT_TOKEN, ...); passing them through made a profile with no
# platforms report, and send through, another profile's bots.
_INHERITED_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM", "TZ",
    "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SYSTEMROOT", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
)


def _child_env(home: str) -> dict:
    env = {k: os.environ[k] for k in _INHERITED_ENV if k in os.environ}
    env["HERMES_HOME"] = str(home)
    env[SUBPROCESS_ENV] = "1"
    return env


def validate_channel(channel: str, home: str) -> tuple[bool, str]:
    """A channel must be a real, connected `hermes send` platform on THIS
    profile. Empty = dashboard-only mode (valid). Prevents invented targets
    like 'dashboard' or 'here' that can never deliver."""
    channel = (channel or "").strip()
    if not channel:
        return True, ""
    platform = channel.split(":")[0].strip().lower()
    detected = list_targets(home)
    if detected is None:
        return False, (
            "Couldn't list this profile's delivery platforms just now (`hermes send --list` "
            "failed), so the channel can't be checked. Try again in a moment, or leave the "
            "channel unset for dashboard-only mode."
        )
    if not detected:
        return False, (
            "No delivery platform is connected on this profile yet, so pings can't be sent. "
            "Connect one first (e.g. `hermes gateway setup`), or leave the channel unset for "
            "dashboard-only mode — dispatches then wait on the dashboard to be read."
        )
    if platform not in detected:
        return False, (
            f"'{platform}' is not a connected delivery platform on this profile. "
            f"Detected: {', '.join(detected)}. Use one of those, or leave the channel unset "
            "for dashboard-only mode. The dashboard is a place to read dispatches, not a send target."
        )
    return True, ""


def _send_error(stdout: str) -> str:
    """The real reason from `hermes send --json`: its error plus any warnings
    (the cause behind an error). '' when the output isn't the JSON shape."""
    try:
        payload = json.loads(stdout or "")
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    parts = [str(w) for w in payload.get("warnings") or ()]
    if payload.get("error"):
        parts.insert(0, str(payload["error"]))
    return "\n".join(parts)


def send(channel: str, text: str, home: str, dispatch_id: str) -> tuple[bool | None, str]:
    """Send via hermes send. Always records to the outbox, send or no send.

    Returns (True, "") on delivery, (False, reason) on a definite failure, and
    (None, reason) when the outcome is unknown — the CLI timed out, so the
    child may still deliver. Callers must not retry an unknown outcome.

    Uses --json, never --quiet: --quiet suppresses the CLI's error text, which
    is what made failures look 'silent'."""
    record = {
        "ts": time.time(),
        "dispatch_id": dispatch_id,
        "channel": channel,
        "text": text,
        "delivered": False,
        "detail": "",
    }
    outcome: bool | None = False
    try:
        proc = subprocess.run(
            _hermes_cmd() + ["send", "--to", channel, "--json", text],
            env=_child_env(home), capture_output=True, text=True, timeout=45,
        )
        record["delivered"] = outcome = proc.returncode == 0
        if not record["delivered"]:
            raw = _send_error(proc.stdout) or proc.stderr or proc.stdout or ""
            record["detail"] = _clean_detail(raw) or (
                f"hermes send exited {proc.returncode} with no output"
            )
    except subprocess.TimeoutExpired:
        outcome = None
        record["delivered"] = None
        record["detail"] = "hermes send timed out after 45s; the message may still have been delivered"
    except OSError as exc:
        record["detail"] = str(exc)[:300]
    _outbox_append(record)
    return outcome, record["detail"]


def list_targets(home: str) -> list[str] | None:
    """Delivery platforms configured on this profile, via Hermes's own CLI
    (no .env reads here): the keys of `hermes send --list --json`.

    Returns None when the listing itself fails (timeout, missing CLI,
    non-zero exit, unparseable output) — that is "unknown", never "none
    connected". A configured platform with no discovered channels is still
    listed (Hermes treats it as a valid send target), and platform names
    with underscores (whatsapp_cloud, ...) are kept as-is.
    """
    try:
        proc = subprocess.run(
            _hermes_cmd() + ["send", "--list", "--json"],
            env=_child_env(home), capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        platforms = json.loads(proc.stdout or "").get("platforms")
    except (ValueError, AttributeError):
        return None
    if not isinstance(platforms, dict):
        return None
    return [str(name).strip().lower() for name in platforms if str(name).strip()]
