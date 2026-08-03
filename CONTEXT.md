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
`beta.yml` on every push to `uat`, tagged `beta-<sha>`). Defaults to
`stable`; auto-detected as `beta` on first boot if the running `__version__`
carries a `+beta.<sha>` local segment. Not to be confused with a Docker image
tag or a Health Check category — it is purely the update-detection setting.

## Update Check

The periodic (24h, plus manual "Check now") comparison of the running
instance's version *identity* (not semver ordering) against the latest
GitHub Release for the configured **Release Channel**. Owned by a dedicated
`UpdateCheckScheduler` that mirrors the Health Check Framework's
scheduler/persisted-state/edge-triggered-notification pattern, but an Update
Check is deliberately **not** a Health Check — an available update is not a
failure state (no severity, no Check Code), it is informational.
