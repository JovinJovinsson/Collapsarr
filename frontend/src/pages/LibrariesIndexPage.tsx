import { Library } from "lucide-react";
import { Navigate } from "react-router-dom";

import { readLastVisitedLibraryInstanceId } from "../api/library";
import { useInstances } from "../hooks/useInstances";

/**
 * Landing target for the "Libraries" primary nav item (COL-100). Clicking
 * "Libraries" itself -- as opposed to an already-expanded per-instance
 * sub-item in the sidebar -- has no instance of its own to show, so it
 * redirects to one: the last-visited instance recorded in localStorage
 * (`readLastVisitedLibraryInstanceId`, written by `LibraryPage` on a
 * successful load), or the first configured instance if none was recorded
 * yet. `LibraryPage` (mounted at `/libraries/:instanceId`) is what actually
 * renders the tree once a target instance id is known.
 *
 * Instance list comes from `useInstances()` (COL-100 code review), shared
 * with `Sidebar`'s `LibraryNavSection` and `LibraryPage` via
 * `InstancesProvider` in `AppShell` rather than fetched here independently.
 */
export function LibrariesIndexPage() {
  const state = useInstances();

  if (state.status === "loading") {
    return (
      <section className="view">
        <div className="panel panel--empty">
          <p className="panel__message">Loading libraries…</p>
        </div>
      </section>
    );
  }

  if (state.status === "error") {
    return (
      <section className="view">
        <header className="view__header">
          <h1 className="view__title">Libraries</h1>
        </header>
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <Library width={28} height={28} />
          </span>
          <p className="panel__message">Couldn&apos;t load configured instances: {state.message}</p>
        </div>
      </section>
    );
  }

  if (state.instances.length === 0) {
    return (
      <section className="view">
        <header className="view__header">
          <h1 className="view__title">Libraries</h1>
        </header>
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <Library width={28} height={28} />
          </span>
          <p className="panel__message">
            No Sonarr or Radarr instances configured yet. Add one under Settings to browse its
            library.
          </p>
        </div>
      </section>
    );
  }

  const lastVisitedId = readLastVisitedLibraryInstanceId();
  const lastVisited =
    lastVisitedId !== null ? state.instances.find((instance) => instance.id === lastVisitedId) : undefined;
  const target = lastVisited ?? state.instances[0];

  return <Navigate to={`/libraries/${target.id}`} replace />;
}
