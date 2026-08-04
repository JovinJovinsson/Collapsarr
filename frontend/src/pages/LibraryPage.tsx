import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { fetchLibraryTree, rememberVisitedLibraryInstance, updateTracked } from "../api/library";
import { LibraryIcon } from "../components/icons";
import { useInstances } from "../hooks/useInstances";
import type { ArrInstance } from "../types/instances";
import type {
  EpisodeNode,
  LibraryNodeKind,
  LibraryTree,
  MovieNode,
  SeasonNode,
  SeriesNode,
} from "../types/library";
import { isMovieTree } from "../types/library";

type TreeLoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; tree: LibraryTree };

const TYPE_LABEL: Record<ArrInstance["type"], string> = { sonarr: "Sonarr", radarr: "Radarr" };

/** A small "has a file yet?" badge -- the read-only distinction this slice needs (COL-100). */
function FileStatusBadge({ hasFile }: { hasFile: boolean }) {
  return (
    <span
      className={
        hasFile
          ? "library-tree-table__file-badge library-tree-table__file-badge--present"
          : "library-tree-table__file-badge library-tree-table__file-badge--missing"
      }
    >
      {hasFile ? "Has file" : "Missing"}
    </span>
  );
}

/**
 * The single-item Tracked toggle (COL-101): a row-level button reading the
 * node's *resolved* Tracked value and flipping it via `POST
 * /api/library/tracked`. `pending` (this row's own toggle in flight) disables
 * the button and swaps its label so a double-click can't fire two overlapping
 * requests; the row stays showing its pre-toggle value until the request
 * resolves and the tree is re-fetched (`LibraryPage`'s `onToggle`), rather
 * than optimistically flipping ahead of the server -- a Series/Season toggle
 * cascades to descendants the button itself has no knowledge of, so an
 * optimistic local flip would be wrong for every row but this one.
 */
function TrackedToggleButton({
  tracked,
  pending,
  onToggle,
}: {
  tracked: boolean;
  pending: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      className={
        tracked
          ? "library-tree-table__tracked-toggle library-tree-table__tracked-toggle--on"
          : "library-tree-table__tracked-toggle library-tree-table__tracked-toggle--off"
      }
      onClick={onToggle}
      disabled={pending}
      aria-pressed={tracked}
    >
      {pending ? "Updating…" : tracked ? "Tracked" : "Not Tracked"}
    </button>
  );
}

/**
 * Row class list for a leaf node (Episode/Movie): the shared base classes
 * plus `--dimmed` when it has no file yet -- the row-level half of the
 * has-file distinction `FileStatusBadge` carries at the cell level (COL-100
 * AC: "Nodes with no file are visually distinguished... on both instance
 * types"). Shared by `SeriesTree`'s episode rows and `MovieTable`'s rows so
 * the dimming rule lives in exactly one place.
 */
function fileNodeRowClassName(hasFile: boolean, ...baseClasses: string[]): string {
  return [...baseClasses, hasFile ? null : "library-tree-table__row--dimmed"]
    .filter((value): value is string => Boolean(value))
    .join(" ");
}

/** Toggle-expand id set, shared by the Series and Season expand/collapse rows. */
function useExpandable(): [Set<number>, (id: number) => void] {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  function toggle(id: number) {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }
  return [expanded, toggle];
}

/** Shared shape every row-level Tracked toggle needs from `LibraryPage` (COL-101). */
interface TrackedToggleProps {
  pendingNodeId: number | null;
  onToggle: (nodeType: LibraryNodeKind, nodeId: number, nextTracked: boolean) => void;
}

/**
 * The Sonarr Series > Season > Episode tree (COL-100): Series rows expand to
 * their Seasons, Season rows expand to their Episodes. Only Episode rows
 * carry `has_file` (per `collapsarr/library/routes.py`'s schema), so that's
 * the level the file-status badge/dimming applies to. Every row carries its
 * own Tracked toggle (COL-101): toggling a Series or Season row cascades
 * server-side to its descendants, reflected here once the caller's refetch
 * (triggered by `onToggle`) lands with their updated resolved values.
 */
function SeriesTree({ series, pendingNodeId, onToggle }: { series: SeriesNode[] } & TrackedToggleProps) {
  const [expandedSeries, toggleSeries] = useExpandable();
  const [expandedSeasons, toggleSeason] = useExpandable();

  return (
    <div className="panel library-tree-panel">
      <table className="library-tree-table">
        <thead>
          <tr>
            <th scope="col">Title</th>
            <th scope="col">File</th>
            <th scope="col">Tracked</th>
          </tr>
        </thead>
        <tbody>
          {series.map((seriesNode) => {
            const seriesOpen = expandedSeries.has(seriesNode.id);
            return (
              <Fragment key={seriesNode.id}>
                <tr className="library-tree-table__row library-tree-table__row--series">
                  <td>
                    <button
                      type="button"
                      className="library-tree-table__toggle"
                      onClick={() => toggleSeries(seriesNode.id)}
                      aria-expanded={seriesOpen}
                    >
                      <span className="library-tree-table__caret" aria-hidden>
                        {seriesOpen ? "▾" : "▸"}
                      </span>
                      {seriesNode.title}
                    </button>
                  </td>
                  <td />
                  <td>
                    <TrackedToggleButton
                      tracked={seriesNode.tracked}
                      pending={pendingNodeId === seriesNode.id}
                      onToggle={() => onToggle("series", seriesNode.id, !seriesNode.tracked)}
                    />
                  </td>
                </tr>
                {seriesOpen &&
                  seriesNode.seasons.map((seasonNode: SeasonNode) => {
                    const seasonOpen = expandedSeasons.has(seasonNode.id);
                    return (
                      <Fragment key={seasonNode.id}>
                        <tr className="library-tree-table__row library-tree-table__row--season">
                          <td className="library-tree-table__cell--indent-1">
                            <button
                              type="button"
                              className="library-tree-table__toggle"
                              onClick={() => toggleSeason(seasonNode.id)}
                              aria-expanded={seasonOpen}
                            >
                              <span className="library-tree-table__caret" aria-hidden>
                                {seasonOpen ? "▾" : "▸"}
                              </span>
                              Season {seasonNode.season_number}
                            </button>
                          </td>
                          <td />
                          <td>
                            <TrackedToggleButton
                              tracked={seasonNode.tracked}
                              pending={pendingNodeId === seasonNode.id}
                              onToggle={() => onToggle("season", seasonNode.id, !seasonNode.tracked)}
                            />
                          </td>
                        </tr>
                        {seasonOpen &&
                          seasonNode.episodes.map((episode: EpisodeNode) => (
                            <tr
                              key={episode.id}
                              className={fileNodeRowClassName(
                                episode.has_file,
                                "library-tree-table__row",
                                "library-tree-table__row--episode",
                              )}
                            >
                              <td className="library-tree-table__cell--indent-2">
                                E{String(episode.episode_number).padStart(2, "0")} &middot; {episode.title}
                              </td>
                              <td>
                                <FileStatusBadge hasFile={episode.has_file} />
                              </td>
                              <td>
                                <TrackedToggleButton
                                  tracked={episode.tracked}
                                  pending={pendingNodeId === episode.id}
                                  onToggle={() => onToggle("episode", episode.id, !episode.tracked)}
                                />
                              </td>
                            </tr>
                          ))}
                      </Fragment>
                    );
                  })}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** The flat Radarr Movie list (COL-99/COL-100): no Season/Episode nesting. */
function MovieTable({ movies, pendingNodeId, onToggle }: { movies: MovieNode[] } & TrackedToggleProps) {
  return (
    <div className="panel library-tree-panel">
      <table className="library-tree-table">
        <thead>
          <tr>
            <th scope="col">Title</th>
            <th scope="col">File</th>
            <th scope="col">Tracked</th>
          </tr>
        </thead>
        <tbody>
          {movies.map((movie) => (
            <tr key={movie.id} className={fileNodeRowClassName(movie.has_file, "library-tree-table__row")}>
              <td>{movie.title}</td>
              <td>
                <FileStatusBadge hasFile={movie.has_file} />
              </td>
              <td>
                <TrackedToggleButton
                  tracked={movie.tracked}
                  pending={pendingNodeId === movie.id}
                  onToggle={() => onToggle("movie", movie.id, !movie.tracked)}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * An instance's Library browsing page (COL-100/COL-101), the per-instance page
 * every Libraries sidebar sub-item and the `LibrariesIndexPage` redirect
 * ultimately land on. Sourced from `GET /api/library/instances/{id}/tree`
 * (COL-98/COL-99): renders the Series > Season > Episode tree for a Sonarr
 * instance, or the flat Movie table for a Radarr one. Every row's Tracked
 * toggle (COL-101) calls `POST /api/library/tracked` with a single node
 * reference, then re-fetches the tree so a Series/Season toggle's
 * server-side cascade to its descendants is reflected across every affected
 * row without a manual page refresh.
 *
 * On a successful load, records this instance as the last-visited one
 * (`rememberVisitedLibraryInstance`) so a later bare "Libraries" click
 * returns here.
 *
 * The instance's own metadata (name/type/base_url, for the page header)
 * comes from `useInstances()` (COL-100 code review), shared with `Sidebar`'s
 * `LibraryNavSection` and `LibrariesIndexPage` via `InstancesProvider` in
 * `AppShell` -- this page no longer fetches its own copy of the full
 * instance list just to look up one entry.
 */
export function LibraryPage() {
  const { instanceId: instanceIdParam } = useParams<{ instanceId: string }>();
  const instanceId = Number(instanceIdParam);

  const [treeState, setTreeState] = useState<TreeLoadState>({ status: "loading" });
  const [pendingNodeId, setPendingNodeId] = useState<number | null>(null);
  const [toggleError, setToggleError] = useState<string | null>(null);
  const instancesState = useInstances();

  // Monotonic token guarding against a stale response landing after a newer
  // one (e.g. `instanceId` changes again, or a toggle's own refetch races an
  // in-flight one) -- replaces the original effect-local `cancelled` flag
  // now that both the instance-driven load and the toggle-driven reload
  // share this one function.
  const loadTokenRef = useRef(0);

  const loadTree = useCallback(async (): Promise<void> => {
    if (!Number.isInteger(instanceId)) {
      setTreeState({ status: "error", message: "Invalid instance id." });
      return;
    }
    const requestToken = ++loadTokenRef.current;
    try {
      const tree = await fetchLibraryTree(instanceId);
      if (loadTokenRef.current !== requestToken) return;
      setTreeState({ status: "ready", tree });
      rememberVisitedLibraryInstance(instanceId);
    } catch (error: unknown) {
      if (loadTokenRef.current !== requestToken) return;
      setTreeState({
        status: "error",
        message: error instanceof Error ? error.message : "Unknown error.",
      });
    }
  }, [instanceId]);

  useEffect(() => {
    setTreeState({ status: "loading" });
    void loadTree();
  }, [loadTree]);

  async function handleToggle(nodeType: LibraryNodeKind, nodeId: number, nextTracked: boolean) {
    setToggleError(null);
    setPendingNodeId(nodeId);
    try {
      await updateTracked(nodeType, nodeId, nextTracked);
      // Re-fetch rather than patch local state: a Series/Season toggle
      // cascades to descendants server-side, and this is the one place that
      // already knows how to render the resulting tree correctly.
      await loadTree();
    } catch (error: unknown) {
      setToggleError(error instanceof Error ? error.message : "Unknown error.");
    } finally {
      setPendingNodeId(null);
    }
  }

  const instance =
    instancesState.status === "ready"
      ? (instancesState.instances.find((candidate) => candidate.id === instanceId) ?? null)
      : null;

  return (
    <section className="view">
      <header className="view__header">
        <h1 className="view__title">{instance ? instance.name : "Library"}</h1>
        <p className="view__summary">
          {instance
            ? `${TYPE_LABEL[instance.type]} library — ${instance.base_url}`
            : "Series/Season/Episode or Movie catalog for this instance."}
        </p>
      </header>

      {toggleError && <p className="form-error">Couldn&apos;t update Tracked: {toggleError}</p>}

      {treeState.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading library…</p>
        </div>
      )}

      {treeState.status === "error" && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <LibraryIcon width={28} height={28} />
          </span>
          <p className="panel__message">Couldn&apos;t load this library: {treeState.message}</p>
        </div>
      )}

      {treeState.status === "ready" &&
        !isMovieTree(treeState.tree) &&
        (treeState.tree.series.length === 0 ? (
          <div className="panel panel--empty">
            <span className="panel__icon" aria-hidden>
              <LibraryIcon width={28} height={28} />
            </span>
            <p className="panel__message">No series found in this library yet.</p>
          </div>
        ) : (
          <SeriesTree series={treeState.tree.series} pendingNodeId={pendingNodeId} onToggle={handleToggle} />
        ))}

      {treeState.status === "ready" &&
        isMovieTree(treeState.tree) &&
        (treeState.tree.movies.length === 0 ? (
          <div className="panel panel--empty">
            <span className="panel__icon" aria-hidden>
              <LibraryIcon width={28} height={28} />
            </span>
            <p className="panel__message">No movies found in this library yet.</p>
          </div>
        ) : (
          <MovieTable movies={treeState.tree.movies} pendingNodeId={pendingNodeId} onToggle={handleToggle} />
        ))}
    </section>
  );
}
