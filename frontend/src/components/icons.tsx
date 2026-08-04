import type { SVGProps } from "react";

/**
 * Minimal line icons drawn on a 24x24 grid, matching the *arr family's spare
 * monoline sidebar iconography. `currentColor` lets the nav drive their tint.
 */
const base: SVGProps<SVGSVGElement> = {
  width: 20,
  height: 20,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
};

// Wanted: a target / magnifying focus on what's still missing.
export function WantedIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <circle cx="11" cy="11" r="7" />
      <path d="M11 8v3l2 2" />
    </svg>
  );
}

// Activity: a pulse / history waveform.
export function ActivityIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M3 12h4l2-6 4 12 2-6h6" />
    </svg>
  );
}

// Settings: a gear.
export function SettingsIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4l1.4-1.4M18 6l1.4-1.4" />
    </svg>
  );
}

// Backups: stacked database cylinders — a saved snapshot of the store.
export function BackupIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <ellipse cx="12" cy="5" rx="7" ry="2.5" />
      <path d="M5 5v6c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5V5" />
      <path d="M5 11v6c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5v-6" />
    </svg>
  );
}

// Warning: an alert triangle, used by the app-health banner (COL-38) for
// warning-severity entries and the Health Checks page's warning badge (COL-76).
export function WarningIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M12 3.5 2.5 20h19L12 3.5Z" />
      <path d="M12 9.5v5" />
      <circle cx="12" cy="17" r="0.75" fill="currentColor" stroke="none" />
    </svg>
  );
}

// Error: an alert octagon, distinct from WarningIcon's triangle -- used by the
// app-health banner (COL-76) and the Health Checks page for error-severity
// entries, so warning vs error reads as a shape difference, not just colour.
export function ErrorIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M8 3h8l5 5v8l-5 5H8l-5-5V8l5-5Z" />
      <path d="M12 8.5v5" />
      <circle cx="12" cy="16.5" r="0.75" fill="currentColor" stroke="none" />
    </svg>
  );
}

// Health: a pulse inside a cross, distinguishing the System > Health page
// from Activity's plain waveform (COL-76).
export function HealthIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M9 4.5h6v4h4v6h-4v4H9v-4H5v-6h4v-4Z" />
      <path d="M7.5 11.5h2l1-2 2 4 1-2h3" />
    </svg>
  );
}

// Update: a downward arrow into a tray -- a neutral "new version available"
// glyph, deliberately unlike WarningIcon/ErrorIcon's alert shapes (COL-87);
// used by the System > Updates page and the app-wide update indicator.
export function UpdateIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M12 3.5v10.5" />
      <path d="M8 10.5 12 14.5 16 10.5" />
      <path d="M4.5 17.5v1.5a1.5 1.5 0 0 0 1.5 1.5h12a1.5 1.5 0 0 0 1.5-1.5v-1.5" />
    </svg>
  );
}

// Wordmark glyph: converging channels funneling into two bars, echoing the
// downmix concept. Placeholder mark only — final logo lands in COL-9.
export function BrandMark(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      width={26}
      height={26}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      {...props}
    >
      <path d="M4 5l6 5-6 5" />
      <path d="M12 5l6 5-6 5" />
      <path d="M20 8v8" />
    </svg>
  );
}
