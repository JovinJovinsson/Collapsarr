# Security Policy

Collapsarr is self-hosted software that stores authentication credentials and
API keys for other services (e.g. Radarr) on your behalf. Security issues are
taken seriously — please report them privately rather than through a public
issue.

## Supported versions

Collapsarr is pre-1.0. Only the **latest tagged release** on `main` receives
security fixes — there is no long-term-support branch. If you're on an older
version, upgrade and confirm the issue still reproduces before reporting.
[Beta builds](https://github.com/JovinJovinsson/Collapsarr/wiki/Beta-Builds)
(the `uat` branch's Docker `beta` tag / GitHub Pre-releases) are pre-release
code by definition — please still report anything you find there, but expect
it to move faster and be less stable than a tagged release.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: go to the
[Security tab](https://github.com/JovinJovinsson/Collapsarr/security) and
click **"Report a vulnerability"**. This opens a private advisory visible
only to the maintainer and you — do not open a public issue or PR for a
suspected vulnerability.

Please include:

- The version/commit or image tag affected.
- Steps to reproduce, or a minimal PoC.
- What you'd expect to happen vs. what actually happens.
- Impact as you see it (e.g. what an attacker could do with this).

You should get an initial response within **7 days**. If it's confirmed,
we'll work out a fix and disclosure timeline together — the reporter is
credited in the advisory and release notes unless you'd rather stay
anonymous.

## Scope

**In scope:** the Collapsarr application code, its Docker image, and its
release/build pipeline (GitHub Actions workflows in this repo).

**Out of scope:**

- Vulnerabilities in dependencies (Python/npm packages, FFmpeg, base Docker
  images) — please report those upstream; feel free to also flag it here if
  a fixed version needs pulling in.
- Third-party services Collapsarr integrates with (Radarr, etc.) — report
  those to the relevant project.
- The [reverse-proxy authentication caveat already documented in the
  README](https://github.com/JovinJovinsson/Collapsarr#authentication):
  running Collapsarr behind a reverse proxy without setting the Login
  requirement to "Always required" causes every client to be treated as
  local and skip authentication. This is a known, documented limitation
  (trusted-proxy support is planned), not a new report.
