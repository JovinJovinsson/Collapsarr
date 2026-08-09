/**
 * Runtime URL-base resolution for reverse-proxy subpath mounting (COL-118).
 *
 * When Collapsarr is served behind a reverse proxy at a subpath (e.g.
 * `/collapsarr`), the backend injects that prefix into the served `index.html`
 * as `window.__COLLAPSARR_URL_BASE__` before the app bundle loads (see
 * `collapsarr/frontend.py`). Static assets are already relative (Vite
 * `base: "./"`), but absolute-rooted API calls, browser navigation, and
 * redirects all need the prefix re-added -- this module is the single place the
 * runtime value is read and applied.
 *
 * The value is absent in dev (Vite serves at the root) and in tests (jsdom has
 * no injected global), so both `getUrlBase` and `prefixPath` degrade to a
 * no-op empty prefix -- today's unprefixed behaviour.
 */

declare global {
  interface Window {
    /**
     * Reverse-proxy subpath prefix injected by the backend at page load
     * (e.g. `"/collapsarr"`). Absent/empty when Collapsarr is served at the
     * root.
     */
    __COLLAPSARR_URL_BASE__?: string;
  }
}

/**
 * The configured URL base, normalized to either `""` (served at the root) or a
 * leading-slash, no-trailing-slash prefix (e.g. `"/collapsarr"`). The backend
 * already normalizes the value it injects; the trim here is defensive so a
 * stray trailing slash can never double up when prefixing a `/`-rooted path.
 */
export function getUrlBase(): string {
  const raw = globalThis.window?.__COLLAPSARR_URL_BASE__ ?? "";
  if (!raw) {
    return "";
  }
  return raw.replace(/\/+$/, "");
}

/**
 * Re-adds the runtime URL base to an absolute-rooted (`/`-prefixed) path so it
 * resolves under the subpath the app is mounted at. Non-rooted paths (relative
 * URLs, absolute `http(s)://` URLs) and requests made with no configured base
 * are returned unchanged.
 */
export function prefixPath(path: string): string {
  const base = getUrlBase();
  if (!base || !path.startsWith("/")) {
    return path;
  }
  return `${base}${path}`;
}
