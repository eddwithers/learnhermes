---
name: journey
description: "Use when the user asks how LearnHermes works, what dispatches are, or how their learning journey is tracked. Explains the evidence rules, surfaces and settings."
version: 0.1.1
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
setup. Offer ONLY the detected platforms (from `action=status`
`detected_platforms`). If the user picks one, `configure` that channel and
`ping` a first test dispatch.

If the user says "here", "the dashboard", or wants no messaging app, that is
**dashboard-only mode**: leave the channel UNSET. Do NOT configure a channel
named `dashboard`, `here`, or similar — those are not `hermes send` targets
and can never deliver. Tell them dispatches will wait on the LearnHermes
dashboard to read, and they can connect a platform later for push delivery.

If the user declines, respect it — the journey stays available on the
dashboard and via `/dispatches`, and you should not bring it up again
unprompted.

## Delivery failures — never restart the gateway

A failed ping is a **channel problem**, not a gateway or install problem.
`configure` now rejects channels that are not connected platforms on this
profile, and delivery errors are cleaned of unrelated Hermes update warnings.

If a ping fails: read the returned `error`/`reason`, fix the channel (connect
a platform, or switch to dashboard-only), and retry. **Never** run
`hermes gateway restart`, `hermes update`, or any install/service command to
"fix" LearnHermes delivery — those affect every profile on the machine and are
never the remedy for a dispatch that won't send. If the error mentions an
update or gateway restart, ignore that noise; it is not a delivery instruction.

A channel can also be **saved but not connected** (set by an older version, or
left behind after a platform disconnect). Then every surface says so: status
`next_step` flags it, the timing loop pauses with
`channel '<x>' is not connected on this profile`, and `/dispatches`, the
dashboard and the snapshot show a warning. The fix is the same: `configure` a
connected platform, or set the channel to the empty string for dashboard-only
mode. Both write paths (tool and dashboard settings) validate the channel, so
an invalid one cannot be saved again.

If the reason says LearnHermes **couldn't list delivery platforms**, that is
a transient listing failure (the `hermes send --list` check timed out or
errored), not a missing platform — nothing needs changing; the next tick
retries.

If the user is sure a platform is connected but LearnHermes says it isn't,
check where its bot token lives: LearnHermes only sees credentials in the
profile's own `.env` or `config.yaml`, not ones injected through the process
environment (Docker, systemd, launchd). Moving the token into the profile's
`.env` fixes it. Don't work around it by changing how the gateway starts.

## After a LearnHermes update

Plugin code loads into the running gateway at load time. Updating
the files on disk does NOT by itself change the code a running host serves —
behaviour can stay stale until the plugin is reloaded. If LearnHermes was just
updated and something still acts like the old version, say so and tell the
user a reload is needed: the operator can run
`hermes plugins disable learnhermes` then `hermes plugins enable learnhermes`
on this profile, or it loads at the next natural gateway restart. **Never**
restart the gateway or run `hermes update` yourself to force it. Sessions
started before the update also keep their old toolset — `/new` fixes that.

## Answering journey questions

When the user asks how LearnHermes works, where their dispatches are, or
anything about the journey: call `learnhermes_journey` with `action=status`
FIRST. Report their actual progress briefly (milestones done, current
dispatch) and follow the `next_step` guidance — especially when no channel
is configured, which is the usual reason nothing has arrived. A short,
concrete answer beats a general explanation.

## Dashboard access from another device

The `dashboard_link` in the status result is the correct profile-scoped
URL, but it is only reachable as configured:

- `127.0.0.1` / `localhost` works ONLY on the machine running Hermes. If
  the user is on a phone or another computer, that URL points at their own
  device and will fail. Ask which device they are on before handing over
  a link.
- For a phone on the same Wi-Fi, use the machine's LAN address
  (e.g. `http://192.168.x.x:9119/learnhermes?profile=<name>`); for access
  off-LAN, a Tailscale-style address. Update `dashboard_url` via
  `action=configure` so ping links match.
- The dashboard requires sign-in (the user's Hermes dashboard credentials).
- Named profiles need `?profile=<name>` on dashboard URLs; ping links are
  scoped automatically.

## Tool missing in this session

Tools load at session start. If LearnHermes was installed while a session
was already running, `learnhermes_journey` is not available in that
session — tell the user to start a fresh session (`/new` or a new chat).
Never edit `learner.json` by hand to work around it; the dashboard and
`/dispatches` still work.
