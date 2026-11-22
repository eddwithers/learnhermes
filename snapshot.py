"""Snapshot — a self-contained HTML journey page for phones.

No external assets, no scripts, no network: one file that renders the whole
journey and the current dispatch. Served by the dashboard API (/snapshot)
and written to the plugin data dir at send time, ready for a publishing
relay (e.g. artifact-relay) when one is installed.
"""

from __future__ import annotations

import html
import time
from pathlib import Path

from plugins.plugin_storage import plugin_data_dir

_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; margin: 0; }
body { background: #0b1220; color: #e6edf3; font: 16px/1.55 -apple-system, system-ui, sans-serif;
       padding: 20px; max-width: 640px; margin: 0 auto; }
h1 { font-size: 1.3rem; margin-bottom: 4px; }
h2 { font-size: 1.05rem; margin-bottom: 8px; }
.sub { color: #8b98a9; font-size: .85rem; margin-bottom: 18px; }
.card { background: #111a2c; border: 1px solid #23304a; border-radius: 12px; padding: 16px; margin-bottom: 14px; }
.card.current { border-color: #ff4f00; }
.badge { display: inline-block; font-size: .72rem; padding: 2px 9px; border-radius: 999px;
         border: 1px solid #33415e; color: #aab6c8; margin-right: 6px; }
.badge.done { border-color: #2e7d4f; color: #7fd6a4; }
.badge.current { border-color: #ff4f00; color: #ff8a50; }
.row { display: flex; gap: 10px; align-items: baseline; padding: 7px 0; border-bottom: 1px solid #1c2740; }
.row:last-child { border-bottom: 0; }
.row .n { color: #67748a; width: 1.4em; text-align: right; flex: none; }
.row .t { flex: 1; }
.row .e { color: #67748a; font-size: .78rem; }
.why { color: #b7c2d2; font-size: .92rem; margin: 8px 0 12px; }
pre { background: #0d1526; border: 1px solid #23304a; border-radius: 8px; padding: 12px;
      font-size: .85rem; white-space: pre-wrap; font-family: ui-monospace, monospace; margin: 6px 0 12px; }
a { color: #ff8a50; }
.label { font-size: .78rem; color: #8b98a9; text-transform: uppercase; letter-spacing: .04em; }
footer { color: #5c6a80; font-size: .75rem; margin-top: 18px; }
"""

_STATE_LABEL = {"done": "done", "sent": "sent to you", "open": "open", "dismissed": "dismissed"}


def render_html(j: dict) -> str:
    e = html.escape
    rows = []
    for d in j["dispatches"]:
        cls = "done" if d["state"] == "done" else ""
        ev = f'<div class="e">{e(d["evidence"])}</div>' if d["evidence"] else ""
        rows.append(
            f'<div class="row"><span class="n">{d["n"]}</span>'
            f'<span class="t">{e(d["title"])}{ev}</span>'
            f'<span class="badge {cls}">{_STATE_LABEL.get(d["state"], d["state"])}</span></div>'
        )
    current = ""
    c = j.get("current")
    if c:
        current = f"""
<div class="card current">
  <span class="badge current">dispatch {c['n']}</span>
  <h2 style="display:inline">{e(c['title'])}</h2>
  <p class="why">{e(' '.join(c['why'].split()))}</p>
  <div class="label">Starter prompt — paste this to Hermes</div>
  <pre>{e(c['starter_prompt'])}</pre>
  <a href="{e(c['docs_url'])}">Official guide</a>
  <p class="why" style="margin-top:10px">Completion is verified from your real setup — no need to report back.</p>
</div>"""
    targets_list = j.get("gateway_targets", [])
    if j.get("targets_known", True):
        targets = ", ".join(targets_list) or "none detected"
    else:
        targets = "not checked yet"
    channel = (j["settings"].get("channel") or "").strip()
    channel_warning = ""
    if channel and j.get("targets_known", True) and channel.split(":")[0].strip().lower() not in targets_list:
        channel_warning = (
            '<p class="why" style="color:#ff8a50">This channel is not connected on this '
            "profile — pings can't be sent. Pick a connected platform in the dashboard "
            "settings, or clear the channel for dashboard-only mode.</p>"
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LearnHermes — your journey</title><style>{_CSS}</style></head>
<body>
<h1>LearnHermes</h1>
<div class="sub">
  <span class="badge">{j['milestones_done']} / {j['milestones_total']} milestones</span>
  <span class="badge">pace: {e(j['pace'])}</span>
  <span class="badge">{j['sessions_7d']} sessions this week</span>
</div>
{current}
<div class="card"><h2>The journey</h2>{''.join(rows)}</div>
<div class="card">
  <div class="label">Dispatch channel</div>
  <p class="why">{e(j['settings'].get('channel') or 'not set')} · detected platforms: {e(targets)}</p>
  {channel_warning}
</div>
<footer>Generated {time.strftime('%Y-%m-%d %H:%M', time.localtime())} · LearnHermes watches local state only;
milestones are verified from artifacts, never self-reported.</footer>
</body></html>"""


def write_snapshot(j: dict) -> Path:
    p = plugin_data_dir("learnhermes") / "snapshot.html"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_html(j), encoding="utf-8")
    return p
