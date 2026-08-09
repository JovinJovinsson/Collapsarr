import type { ReactNode } from "react";

import {
  ActivityIcon,
  BackupIcon,
  HealthIcon,
  LibraryIcon,
  SettingsIcon,
  StatusIcon,
  TasksIcon,
  UpdateIcon,
  WantedIcon,
} from "../components/icons";
import { ActivityPage } from "../pages/ActivityPage";
import { BackupsPage } from "../pages/BackupsPage";
import { HealthChecksPage } from "../pages/HealthChecksPage";
import { LibrariesIndexPage } from "../pages/LibrariesIndexPage";
import { SettingsPage } from "../pages/SettingsPage";
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
 */
export const navItems: NavItem[] = [
  { to: "/wanted", label: "Wanted", icon: <WantedIcon />, element: <WantedPage /> },
  { to: LIBRARIES_PATH, label: "Libraries", icon: <LibraryIcon />, element: <LibrariesIndexPage /> },
  { to: "/activity", label: "Activity", icon: <ActivityIcon />, element: <ActivityPage /> },
  { to: "/settings", label: "Settings", icon: <SettingsIcon />, element: <SettingsPage /> },
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
 * Updates.
 */
export const systemNavItems: NavItem[] = [
  { to: "/system/tasks", label: "Tasks", icon: <TasksIcon />, element: <TasksPage /> },
  { to: "/system/backups", label: "Backups", icon: <BackupIcon />, element: <BackupsPage /> },
  { to: "/system/health", label: "Health", icon: <HealthIcon />, element: <HealthChecksPage /> },
  { to: "/system/status", label: "Status", icon: <StatusIcon />, element: <StatusPage /> },
  { to: "/system/updates", label: "Updates", icon: <UpdateIcon />, element: <UpdatesPage /> },
];
