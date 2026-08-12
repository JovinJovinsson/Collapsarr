import {
  CakeSlice,
  DatabaseBackup,
  Download,
  FileText,
  FolderClock,
  HeartPulse,
  Info,
  Library,
  ListChecks,
  ListOrdered,
  Settings,
} from "lucide-react";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { navItems, settingsNavItems, systemNavItems } from "../routes/nav";

/** Pulls the lucide component reference off a `NavItem.icon` element, e.g. `<CakeSlice size={20} />` -> `CakeSlice`. */
function iconType(icon: unknown) {
  return (icon as ReactElement).type;
}

/**
 * COL-191: the app's hand-drawn `components/icons.tsx` module was replaced
 * with lucide-react icons across every nav item. These assertions target
 * `routes/nav.tsx`'s data directly (the single source of truth both the
 * router and the sidebar read from) rather than the rendered DOM, since two
 * of the three nav lists here (`systemNavItems`/`settingsNavItems`) only ever
 * render their *labels* in the sidebar's expanded sub-nav -- `NavSection`
 * doesn't render a `NavItem`'s `icon` for sub-items, only for the group
 * header itself (see `systemNav.test.tsx`/`settingsNav.test.tsx` for that
 * DOM-level coverage) -- so asserting against the data model is what actually
 * exercises "renders the new icon component" for every item this ticket's
 * acceptance criteria calls out.
 */
describe("nav icons (COL-191)", () => {
  it("gives Queue and History visually distinct icons (no longer the shared ActivityIcon)", () => {
    const queue = navItems.find((item) => item.label === "Queue");
    const history = navItems.find((item) => item.label === "History");
    expect(queue).toBeDefined();
    expect(history).toBeDefined();
    expect(iconType(queue!.icon)).toBe(ListOrdered);
    expect(iconType(history!.icon)).toBe(FolderClock);
    expect(iconType(queue!.icon)).not.toBe(iconType(history!.icon));
  });

  it("renders CakeSlice for Wanted", () => {
    const wanted = navItems.find((item) => item.label === "Wanted");
    expect(iconType(wanted!.icon)).toBe(CakeSlice);
  });

  it("renders a lucide icon for every primary nav item", () => {
    const expected: Record<string, unknown> = {
      Wanted: CakeSlice,
      Libraries: Library,
      Queue: ListOrdered,
      History: FolderClock,
    };
    for (const item of navItems) {
      expect(iconType(item.icon)).toBe(expected[item.label]);
    }
  });

  it("renders a lucide icon for every System sub-nav item", () => {
    const expected: Record<string, unknown> = {
      Tasks: ListChecks,
      Backups: DatabaseBackup,
      Health: HeartPulse,
      Status: Info,
      Updates: Download,
      Logs: FileText,
    };
    for (const item of systemNavItems) {
      expect(iconType(item.icon)).toBe(expected[item.label]);
    }
  });

  it("renders the Settings icon for every Settings sub-nav item", () => {
    for (const item of settingsNavItems) {
      expect(iconType(item.icon)).toBe(Settings);
    }
  });
});
