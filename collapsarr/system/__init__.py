"""Cross-scheduler System views (COL-122): the Scheduled Task registry.

Distinct from the per-domain ``collapsarr.health``/``collapsarr.backup``/
``collapsarr.update_check``/``collapsarr.jobs`` packages, which each own
their own scheduler and routes -- this package aggregates *across* them (see
:mod:`collapsarr.system.tasks`), without touching any of their internals
(``docs/adr/0005-system-tasks-endpoint-not-shared-scheduler.md``).
"""

from __future__ import annotations
