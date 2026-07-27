import { useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";

import type { ReactNode } from "react";

import { logout } from "../api/auth";
import type { NavItem } from "../routes/nav";
import { navItems, systemNavItems } from "../routes/nav";
import { BrandMark } from "./icons";

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
        <span className="sidebar__brand-mark" aria-hidden>
          <BrandMark />
        </span>
        <span className="sidebar__brand-name">Collapsarr</span>
      </div>

      <ul className="sidebar__nav">
        {navItems.map((item) => (
          <NavLinkItem key={item.to} {...item} />
        ))}

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
        <span className="sidebar__version">v0.1.0</span>
      </div>
    </nav>
  );
}
