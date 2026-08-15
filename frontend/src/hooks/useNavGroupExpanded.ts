import { useLocation } from "react-router-dom";

/**
 * True while the current route is a sidebar nav group's own base path or
 * nested under it (e.g. `/system` or any `/system/*` route) -- the
 * expand-on-select rule shared by every collapsible sidebar nav group.
 *
 * Extracted from `LibraryNavSection` (COL-100), which established the
 * pattern for Libraries' dynamically-fetched per-instance sub-items; COL-141
 * reuses it for `NavSection`'s statically-known sub-item lists (System, and
 * from COL-142 on, Settings) so both stay in lockstep rather than drifting.
 */
export function useNavGroupExpanded(basePath: string): boolean {
  const location = useLocation();
  return location.pathname === basePath || location.pathname.startsWith(`${basePath}/`);
}
