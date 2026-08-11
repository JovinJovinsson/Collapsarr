import type { ReactNode } from "react";

import {
  ActivityIcon,
  BackupIcon,
  HealthIcon,
  LibraryIcon,
  LogsIcon,
  SettingsIcon,
  StatusIcon,
  TasksIcon,
  UpdateIcon,
  WantedIcon,
} from "../components/icons";
import { BackupsPage } from "../pages/BackupsPage";
import { HealthChecksPage } from "../pages/HealthChecksPage";
import { LibrariesIndexPage } from "../pages/LibrariesIndexPage";
import { LogsPage } from "../pages/LogsPage";
import { QueuePage } from "../pages/QueuePage";
import { SettingsConnectPage } from "../pages/SettingsConnectPage";
import { SettingsGeneralPage } from "../pages/SettingsGeneralPage";
import { SettingsLoggingPage } from "../pages/SettingsLoggingPage";
import { SettingsRadarrPage } from "../pages/SettingsRadarrPage";
import { SettingsSchedulerPage } from "../pages/SettingsSchedulerPage";
import { SettingsSonarrPage } from "../pages/SettingsSonarrPage";
import { SettingsTargetsPage } from "../pages/SettingsTargetsPage";
import { StatusPage } from "../pages/StatusPage";
import { TasksPage } from "../pages/TasksPage";
import { UpdatesPage } from "../pages/UpdatesPage";
import { WantedPage } from "../pages/WantedPage";

/**
 * Path of the "Libraries" primary nav item (COL-100) -- pulled out as a
 * constant since, unlike every other item, both `Sidebar` (to special-case
 * its expandable per-instance rendering) and `router.tsx` (to wire the
 * `/libraries/:instanceId` route alongside it) need to recognise it
 * specifically rather than just iterating `navItems` generically.
 */
export const LIBRARIES_PATH = "/libraries";

/**
 * Base path of the "System" nav group (COL-63) -- `Sidebar` (COL-141) and
 * `router.tsx` both need it: the former as `NavSection`'s `to` (its default
 * link + the expand-on-select base path), the latter for the bare `/system`
 * redirect wired alongside `systemNavItems`' own routes.
 */
export const SYSTEM_PATH = "/system";

/**
 * Base path of the "Settings" nav group. Now that every section has migrated
 * off the old composed `SettingsPage` (COL-142/COL-143/COL-144), this behaves
 * exactly like `SYSTEM_PATH`: a bare route that redirects to the sub-nav's
 * first entry (wired in `router.tsx`), and the `NavSection` `to`/expand base
 * both Sidebar and this module use -- no more of the narrower
 * `SETTINGS_GENERAL_PATH`-as-`to` workaround COL-142/COL-143 needed while the
 * old page still lived at this path. Kept as a constant, mirroring
 * `LIBRARIES_PATH`/`SYSTEM_PATH`, so `Sidebar`, `router.tsx`, and this
 * module's own `settingsNavItems` below can't drift on the literal string.
 */
export const SETTINGS_PATH = "/settings";

/** Path of the Settings sub-nav's General entry (COL-142). */
export const SETTINGS_GENERAL_PATH = `${SETTINGS_PATH}/general`;

/** Path of the Settings sub-nav's Sonarr entry (COL-144). */
export const SETTINGS_SONARR_PATH = `${SETTINGS_PATH}/sonarr`;

/** Path of the Settings sub-nav's Radarr entry (COL-144). */
export const SETTINGS_RADARR_PATH = `${SETTINGS_PATH}/radarr`;

/** Path of the Settings sub-nav's Targets entry (COL-143). */
export const SETTINGS_TARGETS_PATH = `${SETTINGS_PATH}/targets`;

/** Path of the Settings sub-nav's Connect entry (COL-143). */
export const SETTINGS_CONNECT_PATH = `${SETTINGS_PATH}/connect`;

/** Path of the Settings sub-nav's Scheduler entry (COL-146). */
export const SETTINGS_SCHEDULER_PATH = `${SETTINGS_PATH}/scheduler`;

/** Path of the Settings sub-nav's Logging entry (COL-147). */
export const SETTINGS_LOGGING_PATH = `${SETTINGS_PATH}/logging`;

export interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  element: ReactNode;
}

/**
 * Single source of truth for primary navigation. Both the router (route
 * definitions) and the sidebar (links) read from this, so adding a view is a
 * one-line change here. Kept in its own module to avoid a router <-> sidebar
 * import cycle.
 *
 * Settings isn't listed here (COL-144): like System, it's an expandable
 * `NavSection` group rather than a single destination/page component, so
 * `Sidebar` renders it directly (mirroring how System is rendered) and
 * `router.tsx` wires its bare-path redirect + `settingsNavItems` explicitly
 * rather than through this array.
 */
export const navItems: NavItem[] = [
  { to: "/wanted", label: "Wanted", icon: <WantedIcon />, element: <WantedPage /> },
  { to: LIBRARIES_PATH, label: "Libraries", icon: <LibraryIcon />, element: <LibrariesIndexPage /> },
  // Was "Activity" (the combined live-queue + history table). COL-178
  // repurposed this route into the live-only Queue view; COL-176 gives
  // terminal (succeeded/failed) job history its own dedicated page/nav entry.
  { to: "/queue", label: "Queue", icon: <ActivityIcon />, element: <QueuePage /> },
];

/**
 * The **System** nav area (COL-63): operational, install-level views separate
 * from the media workflow above. Backups was its first page; COL-76 adds
 * Health, listing every registered health check's current state; COL-87 adds
 * Updates, comparing the running version against the latest release; COL-122
 * adds Tasks, the Scheduled Task registry aggregating all four background
 * schedulers; COL-123 adds Status, an "About" panel of runtime/environment
 * facts. Rendered as its own labelled section in the sidebar and wired under
 * `/system/*` in the router, both from this single list. COL-126 reorders to
 * match Bazarr's own System sub-nav ordering: Tasks, Backups, Health, Status,
 * Updates. COL-131 adds Logs, a tail view of the current rotating log file
 * (COL-128), appended as the last entry rather than folded into the reorder
 * above.
 */
export const systemNavItems: NavItem[] = [
  { to: `${SYSTEM_PATH}/tasks`, label: "Tasks", icon: <TasksIcon />, element: <TasksPage /> },
  { to: `${SYSTEM_PATH}/backups`, label: "Backups", icon: <BackupIcon />, element: <BackupsPage /> },
  { to: `${SYSTEM_PATH}/health`, label: "Health", icon: <HealthIcon />, element: <HealthChecksPage /> },
  { to: `${SYSTEM_PATH}/status`, label: "Status", icon: <StatusIcon />, element: <StatusPage /> },
  { to: `${SYSTEM_PATH}/updates`, label: "Updates", icon: <UpdateIcon />, element: <UpdatesPage /> },
  { to: `${SYSTEM_PATH}/logs`, label: "Logs", icon: <LogsIcon />, element: <LogsPage /> },
];

/**
 * The **Settings** sub-nav (COL-139/COL-142/COL-143/COL-144/COL-146/COL-147):
 * mirrors `systemNavItems`' shape, splitting the old composed `SettingsPage`
 * into Bazarr-style dedicated pages one at a time. General (COL-142) was the
 * first migrated page; Targets and Connect (COL-143) were the second and
 * third; Sonarr and Radarr (COL-144) split the old combined Sonarr/Radarr
 * instances table into two type-scoped pages (`SettingsSonarrPage`/
 * `SettingsRadarrPage`, both rendering `InstancesSection` fixed to one
 * `type`). Scheduler (COL-146, Phase 2) moves the editable "Backup schedule"
 * panel off `BackupsPage` and adds a read-only cadence readout for the other
 * three periodic tasks. Logging (COL-147) is the seventh and last: it moves
 * the `log_level` runtime-override control off `GeneralSection` onto its own
 * page. This is the final order: General, Sonarr, Radarr, Targets, Connect,
 * Scheduler, Logging -- the old composed `SettingsPage` and its `/settings`
 * route are gone; a bare `/settings` redirects to `SETTINGS_GENERAL_PATH`
 * instead (wired in `router.tsx`, mirroring the bare `/system` redirect).
 */
export const settingsNavItems: NavItem[] = [
  { to: SETTINGS_GENERAL_PATH, label: "General", icon: <SettingsIcon />, element: <SettingsGeneralPage /> },
  { to: SETTINGS_SONARR_PATH, label: "Sonarr", icon: <SettingsIcon />, element: <SettingsSonarrPage /> },
  { to: SETTINGS_RADARR_PATH, label: "Radarr", icon: <SettingsIcon />, element: <SettingsRadarrPage /> },
  { to: SETTINGS_TARGETS_PATH, label: "Targets", icon: <SettingsIcon />, element: <SettingsTargetsPage /> },
  { to: SETTINGS_CONNECT_PATH, label: "Connect", icon: <SettingsIcon />, element: <SettingsConnectPage /> },
  { to: SETTINGS_SCHEDULER_PATH, label: "Scheduler", icon: <SettingsIcon />, element: <SettingsSchedulerPage /> },
  { to: SETTINGS_LOGGING_PATH, label: "Logging", icon: <SettingsIcon />, element: <SettingsLoggingPage /> },
];
