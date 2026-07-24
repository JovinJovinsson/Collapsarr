import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { fetchInstances } from "../api/instances";
import { fetchSettings } from "../api/settings";

const DISMISSED_STORAGE_KEY = "collapsarr.onboardingDismissed";

/** Reads whether the onboarding panel has already been dismissed in this browser. */
function isDismissed(): boolean {
  try {
    return globalThis.localStorage?.getItem(DISMISSED_STORAGE_KEY) === "true";
  } catch {
    // Storage can throw in locked-down environments -- treat as "not dismissed".
    return false;
  }
}

/** Persists the dismissal so the panel stays hidden across reloads/navigation. */
function persistDismissed(): void {
  try {
    globalThis.localStorage?.setItem(DISMISSED_STORAGE_KEY, "true");
  } catch {
    // Best-effort; nothing sensible to do if storage is unavailable.
  }
}

type LoadState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; apiKey: string; configured: boolean };

/**
 * Post-setup onboarding panel (COL-54): rendered in `AppShell` (same spot as
 * `HealthBanner`) so it's visible no matter which view is active once a
 * fresh install makes it past `/setup`/`/login` into the app. Surfaces the
 * auto-generated API key -- also visible read-only in Settings > General,
 * `GeneralSection` -- and a link into Settings to connect the first
 * Sonarr/Radarr instance.
 *
 * "Unconfigured" is defined as "no arr instance configured yet"
 * (`GET /api/instances` empty, COL-27); once one exists the panel has
 * nothing left to prompt for and stops rendering even if never dismissed.
 * Dismissal itself is remembered in `localStorage` under its own key --
 * following the same "best-effort, storage may be unavailable" pattern as
 * the stored API key in `api/client.ts` -- so it stays dismissed across
 * reloads and navigation.
 *
 * Renders nothing while loading or on a fetch error, matching `HealthBanner`'s
 * "fail quiet" stance: a transient network hiccup shouldn't block the rest of
 * the app shell from rendering.
 */
export function OnboardingPanel() {
  const [dismissed, setDismissedState] = useState(isDismissed);
  const [state, setState] = useState<LoadState>({ status: "loading" });

  useEffect(() => {
    if (dismissed) {
      return;
    }
    let cancelled = false;
    Promise.all([fetchSettings(), fetchInstances()])
      .then(([settings, instances]) => {
        if (!cancelled) {
          setState({ status: "ready", apiKey: settings.api_key, configured: instances.length > 0 });
        }
      })
      .catch(() => {
        if (!cancelled) {
          setState({ status: "error" });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [dismissed]);

  function handleDismiss() {
    persistDismissed();
    setDismissedState(true);
  }

  if (dismissed || state.status !== "ready" || state.configured) {
    return null;
  }

  return (
    <div className="onboarding-panel" role="region" aria-label="Getting started">
      <div className="onboarding-panel__body">
        <h2 className="onboarding-panel__title">Welcome to Collapsarr</h2>
        <p className="onboarding-panel__message">
          Your auto-generated API key is <code>{state.apiKey}</code>. Next,{" "}
          <Link to="/settings">connect your first Sonarr or Radarr instance</Link> so Collapsarr
          knows what to track.
        </p>
      </div>
      <button
        type="button"
        className="btn btn--ghost btn--sm onboarding-panel__dismiss"
        onClick={handleDismiss}
      >
        Dismiss
      </button>
    </div>
  );
}
