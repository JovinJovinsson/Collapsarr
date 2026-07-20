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
