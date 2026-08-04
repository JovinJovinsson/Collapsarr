# Domain Glossary — Collapsarr

Terms sharpened during design (`/grill-me`) sessions. Glossary only — no
implementation detail; see `docs/plans/` (or the Confluence **Plans** space)
for the designs that resolved these.

## Health Check

A named, periodic probe of one operational concern (FFmpeg presence, Arr
instance reachability, disk space, database writability, job failure rate),
owned by the Health Check Framework. Distinct from a **Job** (a downmix
task) — a Health Check never touches media files.

## Check Code

The stable, wiki-searchable identifier for a Health Check's failure state,
in the form `{SEVERITY}-{CATEGORY}-{SEQ}` (e.g. `ERR-CONN-001`,
`WARN-DISK-001`). Identifies the *type* of issue, fixed at authoring time —
never derived at runtime and never varies per instance. A check that can
present at two severities (e.g. disk space low vs critical) gets two
distinct codes, not one code with a variable severity.

**Known exception — `ffmpeg_missing`.** The FFmpeg-presence check
(`collapsarr/health/ffmpeg.py`) uses the Check Code `ffmpeg_missing`, which
does *not* follow the format. This is intentional and permanent: it is the
exact code the `/health` liveness endpoint has surfaced since COL-38, and that
value is part of `/health`'s public response contract (documented in
`README.md`, matched on by the frontend health banner). Renaming it would break
that contract, which COL-75's AC6 forbids. This is the sole grandfathered
exception; every new check (including the other COL-74 checks) must use the
`{SEVERITY}-{CATEGORY}-{SEQ}` format — do not copy this shortcut.

## Check Key

The dedup/state identity for a health check result: `(code, instance_id)`
for per-instance checks (e.g. Arr connectivity — one row per Arr instance),
or just `code` for singleton checks (e.g. FFmpeg, disk space, database
writability). A **Check Code** is shared across every instance that fails
the same check; the **Check Key** is what disambiguates which one.

## Check Severity

Exactly two levels: `warning` and `error`. Fixed per **Check Code**, never
computed from live state.

## Dismiss (health check)

Marks a specific, currently-failing **Check Key** as acknowledged (sets
`dismissed_at`). Automatically cleared the next time that Check Key
transitions from passing back to failing — a dismiss silences the *current*
occurrence, not the code forever.

## Release Channel

A `GlobalSettings.update_channel` value (`stable`|`beta`) selecting which
GitHub Release stream the **Update Check** compares the running instance
against. `stable` = the latest non-prerelease Release (cut by `release.yml`
on a `v*.*.*` tag on `main`); `beta` = the latest prerelease Release (cut by
`beta.yml` on every push to `uat`, tagged `beta-v<base>.<build>` — an
orderable next-patch-after-latest-stable base plus a zero-padded
commits-since-that-tag build number, e.g. `beta-v0.2.1.0007`). Defaults to
`stable`; auto-detected as `beta` on first boot if the running `__version__`
carries a bare `+beta` local-version marker. Not to be confused with a Docker
image tag or a Health Check category — it is purely the update-detection
setting.

## Update Check

The periodic (24h, plus manual "Check now") comparison of the running
instance's version *identity* (not semver ordering) against the latest
GitHub Release for the configured **Release Channel**. Owned by a dedicated
`UpdateCheckScheduler` that mirrors the Health Check Framework's
scheduler/persisted-state/edge-triggered-notification pattern, but an Update
Check is deliberately **not** a Health Check — an available update is not a
failure state (no severity, no Check Code), it is informational.

## Install Method

How this instance is running — Docker vs. pipx/bare-metal — detected
server-side via the presence of `/.dockerenv` (the frontend has no
filesystem access to detect it itself). Surfaced as `is_docker` on
`GET /api/system/updates`, and used solely to pick which upgrade
*instructions* the Updates page displays (`docker pull` + recreate-container
vs. `pipx upgrade`/`pip install --upgrade`) — see
`docs/adr/0001-update-check-detect-notify-only.md`. No code path executes
either command; the operator always runs it themselves.

## Wanted (view)

A tracked media file still missing at least one enabled downmix target
(Stereo/2.1/5.1). Nothing to do with Sonarr/Radarr's own "wanted" (missing,
not-yet-downloaded) concept — surfaced by `GET /api/wanted` and the Wanted
sidebar page, driven entirely by per-`(language, target)` status on a
tracked media file. Deliberately distinct from **Tracked**: a file can be
both Tracked and Wanted (eligible for downmixing, and still has a gap), or
Not Tracked and still technically Wanted (it has a gap Collapsarr will never
fill automatically, because the user opted it out).

## Tracked

A user-settable boolean on a **Library Node** (Series, Season, Episode, or
Movie) controlling whether Collapsarr's pipeline should ever act on it
automatically — queue downmix jobs from a scan/webhook, and list it in
**Wanted**. Distinct from Sonarr/Radarr's own `monitored` flag (Collapsarr
never reads or writes it) and from **Wanted** (see above). Setting Tracked
on a Series or Season cascades immediately to every existing descendant,
and also becomes that node's stored default for any child discovered later
— a new episode file landing under a Not-Tracked series defaults to Not
Tracked itself, even if the instance-wide default is Tracked. A Not-Tracked
item can still be downmixed via an explicit manual trigger on its detail
page; Tracked only gates *automatic* behavior. Resolves, in order, from the
nearest explicit ancestor override down to the global `default_tracked`
setting (itself defaulting to `true`) when nothing in a node's ancestry has
been explicitly set.

## Library

A per-`ArrInstance` mirror of that instance's Sonarr/Radarr catalog
(Series/Season/Episode for Sonarr, Movie for Radarr), persisted in
Collapsarr's own database and kept in sync via the same scan/webhook
infrastructure that maintains tracked-media state — never a live proxy to
the Arr API. Includes items with no file yet, in a distinct "no file"
state, so their **Tracked** preference can be set ahead of the file
actually arriving. A Library Node that a later scan no longer sees
(deleted upstream, in Sonarr/Radarr) is hidden rather than deleted,
preserving its Tracked value in case it reappears. One Library exists per
configured `ArrInstance`; the "Libraries" nav item's sidebar sub-items stop
at this level — deeper navigation (Series > Season > Episode) happens
inside a Library's own page, not further nested in the sidebar.

## Library Node

A single entry in a **Library**'s tree: a Series, Season, or Episode
(Sonarr) or a Movie (Radarr), identified by Sonarr/Radarr's own object IDs
rather than parsed from on-disk folder paths. The unit both **Tracked**
status and its cascade/inheritance rules apply to.
