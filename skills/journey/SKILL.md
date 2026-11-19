---
name: journey
description: "Use when the user asks how LearnHermes works, what dispatches are, or how their learning journey is tracked. Explains the evidence rules, surfaces and settings."
version: 0.1.0
author: Edd Coates-Withers
---

# LearnHermes — how the journey works

LearnHermes accompanies a brand-new Hermes user from install to power user.
It watches local state, verifies milestones from artifacts, and sends timed
**dispatches** — short lessons delivered to a channel the user chooses, with
a link to the full lesson on the dashboard.

## The rules that matter

- **Evidence, never claims.** A milestone counts only when the artifact
  exists: a cron job in `cron/jobs.json`, an agent-written skill on disk, a
  session in `state.db`, a named profile. The user never has to report back.
- **Timing.** One dispatch open at a time. Delivery waits for a natural
  pause (no active session, user idle ≥ 15 min), inside the send window,
  under the daily cap, and spaced by pace. Fast-moving users get shorter
  gaps; quiet users get longer ones.
- **The ping is not the lesson.** Notifications carry a short announcement
  plus a link. The dashboard tab (`/learnhermes`) and the mobile snapshot
  (`/api/plugins/learnhermes/snapshot`) carry the full lesson: why it
  matters, a starter prompt, the official guide link.
- **Local only.** Observation reads local state. Nothing is injected into
  the system prompt; prompt caching is untouched. Email, if chosen, is
  one-way — replies are never parsed.

## Surfaces

| Surface | What it does |
|---|---|
| `learnhermes_journey` tool | status / configure / dismiss / ping — the conversational control |
| `/dispatches` slash command | status, send, dismiss, scan |
| Dashboard tab `/learnhermes` | journey map, current dispatch, settings |
| Snapshot page | self-contained HTML for phones |

## Settings

`channel` (any `hermes send` target), `enabled`, `send_start`/`send_end`
(hour window, may wrap midnight), `max_per_day`, `dashboard_url` (must be
reachable from the user's phone for ping links to work there).

## Starting the journey

On the first chat after install, a one-time welcome note asks you to offer
setup. Ask where dispatches should go (offer the detected platforms), then
`configure` the channel and `ping` a first test dispatch. If the user
declines, respect it — the journey stays available on the dashboard and via
`/dispatches`, and you should not bring it up again unprompted.
