import { useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";

import type { ReactNode } from "react";

import { logout } from "../api/auth";
import { useHealth } from "../hooks/useHealth";
import { LIBRARIES_PATH, navItems, systemNavItems } from "../routes/nav";
import type { NavItem } from "../routes/nav";
import { LibraryNavSection } from "./LibraryNavSection";

/** Renders one nav link; shared by the primary and System sections. */
function NavLinkItem({ to, label, icon }: NavItem): ReactNode {
  return (
    <li key={to}>
      <NavLink
        to={to}
        className={({ isActive }) =>
          isActive ? "sidebar__link sidebar__link--active" : "sidebar__link"
        }
      >
        <span className="sidebar__link-icon">{icon}</span>
        <span className="sidebar__link-label">{label}</span>
      </NavLink>
    </li>
  );
}

export function Sidebar() {
  const navigate = useNavigate();
  const [loggingOut, setLoggingOut] = useState(false);

  // Shared `GET /health` state from `HealthProvider` (COL-124 code review) --
  // `HealthBanner` reads the same state, so the app makes that request once
  // rather than each consumer re-fetching independently. `version` stays
  // `null` while the fetch hasn't resolved yet, so the footer renders
  // nothing rather than flashing "Unknown" during normal load; "Unknown" is
  // reserved for an actual fetch failure.
  const health = useHealth();
  const version =
    health.status === "ready" ? health.health.version : health.status === "error" ? "Unknown" : null;

  async function handleLogout() {
    setLoggingOut(true);
    try {
      await logout();
    } finally {
      // Whether or not the request succeeded, send the operator to /login; the
      // server-side gate will re-challenge if the session is somehow still live.
      navigate("/login", { replace: true });
    }
  }

  return (
    <nav className="sidebar" aria-label="Primary">
      <div className="sidebar__brand">
        <img
          className="sidebar__brand-mark"
          src="/apple-touch-icon.png"
          alt=""
          aria-hidden
        />
        <span className="sidebar__brand-name">Collapsarr</span>
      </div>

      <ul className="sidebar__nav">
        {navItems.map((item) =>
          // Libraries (COL-100) has one sub-item per configured ArrInstance
          // rather than a static destination, so it renders through its own
          // expandable section instead of the plain NavLinkItem every other
          // primary nav item uses.
          item.to === LIBRARIES_PATH ? (
            <LibraryNavSection key={item.to} {...item} />
          ) : (
            <NavLinkItem key={item.to} {...item} />
          ),
        )}

        <li className="sidebar__section" aria-hidden>
          System
        </li>
        {systemNavItems.map((item) => (
          <NavLinkItem key={item.to} {...item} />
        ))}
      </ul>

      <div className="sidebar__footer">
        <button
          type="button"
          className="btn btn--ghost btn--sm sidebar__logout"
          onClick={handleLogout}
          disabled={loggingOut}
        >
          {loggingOut ? "Signing out…" : "Sign out"}
        </button>
        {version && <span className="sidebar__version">{version}</span>}
      </div>
    </nav>
  );
}
