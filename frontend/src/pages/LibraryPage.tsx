import { Fragment, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { fetchInstances } from "../api/instances";
import { fetchLibraryTree, rememberVisitedLibraryInstance } from "../api/library";
import { LibraryIcon } from "../components/icons";
import type { ArrInstance } from "../types/instances";
import type { EpisodeNode, LibraryTree, MovieNode, SeasonNode, SeriesNode } from "../types/library";
import { isMovieTree } from "../types/library";

type TreeLoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; tree: LibraryTree };

type InstanceLoadState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; instance: ArrInstance | null };

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

/**
 * The Sonarr Series > Season > Episode tree (COL-100): Series rows expand to
 * their Seasons, Season rows expand to their Episodes. Only Episode rows
 * carry `has_file` (per `collapsarr/library/routes.py`'s schema), so that's
 * the level the file-status badge/dimming applies to.
 */
function SeriesTree({ series }: { series: SeriesNode[] }) {
  const [expandedSeries, toggleSeries] = useExpandable();
  const [expandedSeasons, toggleSeason] = useExpandable();

  return (
    <div className="panel library-tree-panel">
      <table className="library-tree-table">
        <thead>
          <tr>
            <th scope="col">Title</th>
            <th scope="col">File</th>
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
function MovieTable({ movies }: { movies: MovieNode[] }) {
  return (
    <div className="panel library-tree-panel">
      <table className="library-tree-table">
        <thead>
          <tr>
            <th scope="col">Title</th>
            <th scope="col">File</th>
          </tr>
        </thead>
        <tbody>
          {movies.map((movie) => (
            <tr key={movie.id} className={fileNodeRowClassName(movie.has_file, "library-tree-table__row")}>
              <td>{movie.title}</td>
              <td>
                <FileStatusBadge hasFile={movie.has_file} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * An instance's Library browsing page (COL-100), the per-instance page every
 * Libraries sidebar sub-item and the `LibrariesIndexPage` redirect ultimately
 * land on. Sourced from `GET /api/library/instances/{id}/tree`
 * (COL-98/COL-99): renders the Series > Season > Episode tree for a Sonarr
 * instance, or the flat Movie table for a Radarr one -- read-only for this
 * slice, no Tracked toggling yet (COL-101).
 *
 * On a successful load, records this instance as the last-visited one
 * (`rememberVisitedLibraryInstance`) so a later bare "Libraries" click
 * returns here.
 */
export function LibraryPage() {
  const { instanceId: instanceIdParam } = useParams<{ instanceId: string }>();
  const instanceId = Number(instanceIdParam);

  const [treeState, setTreeState] = useState<TreeLoadState>({ status: "loading" });
  const [instanceState, setInstanceState] = useState<InstanceLoadState>({ status: "loading" });

  useEffect(() => {
    if (!Number.isInteger(instanceId)) {
      setTreeState({ status: "error", message: "Invalid instance id." });
      return;
    }
    let cancelled = false;
    setTreeState({ status: "loading" });

    fetchLibraryTree(instanceId)
      .then((tree) => {
        if (cancelled) return;
        setTreeState({ status: "ready", tree });
        rememberVisitedLibraryInstance(instanceId);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setTreeState({
            status: "error",
            message: error instanceof Error ? error.message : "Unknown error.",
          });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [instanceId]);

  useEffect(() => {
    let cancelled = false;

    fetchInstances()
      .then((instances) => {
        if (cancelled) return;
        setInstanceState({
          status: "ready",
          instance: instances.find((candidate) => candidate.id === instanceId) ?? null,
        });
      })
      .catch(() => {
        if (!cancelled) setInstanceState({ status: "error" });
      });

    return () => {
      cancelled = true;
    };
  }, [instanceId]);

  const instance = instanceState.status === "ready" ? instanceState.instance : null;

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
          <SeriesTree series={treeState.tree.series} />
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
          <MovieTable movies={treeState.tree.movies} />
        ))}
    </section>
  );
}
