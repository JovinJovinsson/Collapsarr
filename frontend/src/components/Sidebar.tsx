import { NavLink } from "react-router-dom";

import { navItems } from "../routes/nav";
import { BrandMark } from "./icons";

export function Sidebar() {
  return (
    <nav className="sidebar" aria-label="Primary">
      <div className="sidebar__brand">
        <span className="sidebar__brand-mark" aria-hidden>
          <BrandMark />
        </span>
        <span className="sidebar__brand-name">Collapsarr</span>
      </div>

      <ul className="sidebar__nav">
        {navItems.map(({ to, label, icon }) => (
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
        ))}
      </ul>

      <div className="sidebar__footer">
        <span className="sidebar__version">v0.1.0</span>
      </div>
    </nav>
  );
}
