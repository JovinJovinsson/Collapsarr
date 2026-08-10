import { NavLink } from "react-router-dom";

import type { ReactNode } from "react";

import { useNavGroupExpanded } from "../hooks/useNavGroupExpanded";
import type { NavItem } from "../routes/nav";

export interface NavSectionProps {
  /**
   * Destination of the group's default link, and the base path
   * `useNavGroupExpanded` expands under (this path itself, or anything
   * nested under it). For System this is the group's own base path
   * (`/system`); Settings (COL-142) instead passes its sub-nav's first
   * entry (`/settings/general`), since the group's actual base path
   * (`/settings`) still belongs to the old composed page mid-migration --
   * so don't assume `to` is always the group's literal base path when
   * reusing this component elsewhere.
   */
  to: string;
  label: string;
  /** Optional: the plain `<li className="sidebar__section">` header this
   * replaced (System, pre-COL-141) never had one. */
  icon?: ReactNode;
  /** Statically-known sub-items, e.g. `systemNavItems`. */
  items: NavItem[];
  /** Extra class(es) merged onto the root `<li className="sidebar__nav-group">`. */
  className?: string;
}

/**
 * Shared expand-on-select sidebar nav group (COL-141) for a labelled section
 * with a fixed, statically-known sub-item list: clickable default link +
 * caret + sub-items, expanded only while the current route is under `to`
 * (e.g. any `/system/*` route), collapsed otherwise.
 *
 * Generalizes the pattern `LibraryNavSection` (COL-100) established for
 * Libraries' dynamically-fetched per-instance sub-items -- this is the
 * fixed-`NavItem[]`-list counterpart. Both share the expand rule via
 * `useNavGroupExpanded`; `LibraryNavSection` itself is untouched otherwise
 * since it still needs to render loading/error/empty states a static list
 * never has.
 *
 * First consumer: `Sidebar`'s System section (replacing its previous
 * always-expanded static `<li>` list). From COL-142 on, Settings' sub-nav
 * reuses this same component rather than duplicating the expand/collapse
 * logic.
 */
export function NavSection({ to, label, icon, items, className }: NavSectionProps) {
  const expanded = useNavGroupExpanded(to);

  return (
    <li className={className ? `sidebar__nav-group ${className}` : "sidebar__nav-group"}>
      <NavLink
        to={to}
        className={({ isActive }) =>
          isActive ? "sidebar__link sidebar__link--active" : "sidebar__link"
        }
      >
        {icon && <span className="sidebar__link-icon">{icon}</span>}
        <span className="sidebar__link-label">{label}</span>
        <span className="sidebar__caret" aria-hidden>
          {expanded ? "▾" : "▸"}
        </span>
      </NavLink>

      {expanded && (
        <ul className="sidebar__subnav" aria-label={`${label} sections`}>
          {items.map((item) => (
            <li key={item.to}>
              <NavLink
                to={item.to}
                className={({ isActive }) =>
                  isActive ? "sidebar__sublink sidebar__sublink--active" : "sidebar__sublink"
                }
              >
                {item.label}
              </NavLink>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}
