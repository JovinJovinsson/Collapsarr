import type { ReactNode } from "react";

import { ActivityIcon, BackupIcon, SettingsIcon, WantedIcon } from "../components/icons";
import { ActivityPage } from "../pages/ActivityPage";
import { BackupsPage } from "../pages/BackupsPage";
import { SettingsPage } from "../pages/SettingsPage";
import { WantedPage } from "../pages/WantedPage";

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
  { to: "/activity", label: "Activity", icon: <ActivityIcon />, element: <ActivityPage /> },
  { to: "/settings", label: "Settings", icon: <SettingsIcon />, element: <SettingsPage /> },
];

/**
 * The **System** nav area (COL-63): operational, install-level views separate
 * from the media workflow above. Backups is its first (and, for now, only)
 * page; later slices add the backup interval/retention settings here. Rendered
 * as its own labelled section in the sidebar and wired under `/system/*` in the
 * router, both from this single list.
 */
export const systemNavItems: NavItem[] = [
  { to: "/system/backups", label: "Backups", icon: <BackupIcon />, element: <BackupsPage /> },
];
