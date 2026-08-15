import type { SVGProps } from "react";

/**
 * Wordmark glyph: converging channels funneling into two bars, echoing the
 * downmix concept. Placeholder mark only — final logo lands in COL-9.
 *
 * Split out of the old hand-drawn `components/icons.tsx` module (COL-191):
 * unlike that module's other exports (Wanted/Queue/History/Settings/etc,
 * all migrated to lucide-react icons), this is the app's own brand mark, not
 * a stand-in for a generic concept a library icon set could reasonably
 * represent -- so it stays a bespoke SVG in its own module rather than being
 * mapped onto a lucide icon.
 */
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
