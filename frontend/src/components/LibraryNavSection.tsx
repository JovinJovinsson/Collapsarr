import { NavLink, useLocation } from "react-router-dom";

import { useInstances } from "../hooks/useInstances";
import type { NavItem } from "../routes/nav";

/**
 * Sidebar rendering for the "Libraries" primary nav item (COL-100).
 *
 * Unlike every other primary nav item, its sub-items aren't statically known
 * -- there's one per configured `ArrInstance` -- so it can't be rendered by
 * `Sidebar`'s generic `NavLinkItem` the way Wanted/Activity/Settings are;
 * `Sidebar` special-cases this item to render `LibraryNavSection` instead.
 *
 * Expands to list every configured instance whenever any `/libraries/*`
 * route is active (i.e. on select), collapsed otherwise. Sidebar nesting
 * stops here -- no Series/Season sub-tree in the sidebar itself; that lives
 * on the instance's own page (`LibraryPage`).
 *
 * Instance list comes from `useInstances()` (COL-100 code review), the
 * `GET /api/instances` fetch shared with the Libraries pages via
 * `InstancesProvider` in `AppShell` -- this section no longer fetches its
 * own copy.
 */
export function LibraryNavSection({ to, label, icon }: NavItem) {
  const location = useLocation();
  const expanded = location.pathname === to || location.pathname.startsWith(`${to}/`);

  const state = useInstances();

  return (
    <li className="sidebar__nav-group">
      <NavLink
        to={to}
        className={({ isActive }) =>
          isActive ? "sidebar__link sidebar__link--active" : "sidebar__link"
        }
      >
        <span className="sidebar__link-icon">{icon}</span>
        <span className="sidebar__link-label">{label}</span>
        <span className="sidebar__caret" aria-hidden>
          {expanded ? "▾" : "▸"}
        </span>
      </NavLink>

      {expanded && (
        <ul className="sidebar__subnav" aria-label={`${label} instances`}>
          {state.status === "loading" && <li className="sidebar__subnav-message">Loading…</li>}
          {state.status === "error" && (
            <li className="sidebar__subnav-message">Couldn&apos;t load instances</li>
          )}
          {state.status === "ready" && state.instances.length === 0 && (
            <li className="sidebar__subnav-message">No instances configured</li>
          )}
          {state.status === "ready" &&
            state.instances.map((instance) => (
              <li key={instance.id}>
                <NavLink
                  to={`${to}/${instance.id}`}
                  className={({ isActive }) =>
                    isActive ? "sidebar__sublink sidebar__sublink--active" : "sidebar__sublink"
                  }
                >
                  {instance.name}
                </NavLink>
              </li>
            ))}
        </ul>
      )}
    </li>
  );
}
