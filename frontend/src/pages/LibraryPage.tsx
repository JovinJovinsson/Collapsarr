import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import {
  bulkUpdateTracked,
  fetchLibraryTree,
  rememberVisitedLibraryInstance,
  updateTracked,
} from "../api/library";
import { LibraryIcon } from "../components/icons";
import { TrackedToggleButton } from "../components/TrackedToggleButton";
import { useInstances } from "../hooks/useInstances";
import type { ArrInstance } from "../types/instances";
import type {
  EpisodeNode,
  LibraryNodeKind,
  LibraryTree,
  MovieNode,
  SeasonNode,
  SeriesNode,
  TrackedNodeReference,
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
 * A selection key uniquely identifying one `{node_type, node_id}` reference
 * (COL-103) -- the `Set<string>` LibraryPage keeps its cross-level bulk
 * selection in uses these as members, and the bulk-action toolbar decodes
 * them back into `TrackedNodeReference`s before calling the bulk-update
 * endpoint.
 */
function selectionKey(nodeType: LibraryNodeKind, nodeId: number): string {
  return `${nodeType}:${nodeId}`;
}

/** Reverses `selectionKey` -- see its doc comment. */
function decodeSelectionKey(key: string): TrackedNodeReference {
  const separatorIndex = key.indexOf(":");
  return {
    node_type: key.slice(0, separatorIndex) as LibraryNodeKind,
    node_id: Number(key.slice(separatorIndex + 1)),
  };
}

/**
 * The Tracked-status filter's three states (COL-104): "all" (the default)
 * shows every row regardless of its resolved Tracked value; the other two
 * narrow to rows whose *resolved* Tracked value (the same `tracked` field
 * `resolve_tracked` already stamps on every node server-side, per
 * `collapsarr/library/service.py`) matches.
 */
type TrackedFilterValue = "all" | "tracked" | "not-tracked";

/** True if a node's resolved Tracked value satisfies the selected Tracked filter (COL-104). */
function matchesTrackedFilter(tracked: boolean, filter: TrackedFilterValue): boolean {
  if (filter === "all") return true;
  return filter === "tracked" ? tracked : !tracked;
}

/** Case-insensitive substring match against the search query (COL-104); a blank query matches everything. */
function matchesSearchQuery(title: string, query: string): boolean {
  const needle = query.trim().toLowerCase();
  return needle === "" || title.toLowerCase().includes(needle);
}

/**
 * Filters one Episode leaf (COL-104/COL-104 tracked-filter fix). The Tracked
 * filter is a strict, standalone gate on the Episode's *own* resolved
 * Tracked value -- no ancestor-context carve-out, per AC2's literal
 * wording ("shows only nodes currently resolving to Tracked"). Only once
 * that gate passes does the search side of things run: kept if an ancestor
 * Series title already satisfied the search query (the whole subtree is in
 * scope), or its own title matches the search query.
 */
function filterEpisode(
  episode: EpisodeNode,
  query: string,
  trackedFilter: TrackedFilterValue,
  ancestorSearchSatisfied: boolean,
): EpisodeNode | null {
  if (!matchesTrackedFilter(episode.tracked, trackedFilter)) return null;
  const searchSatisfied = ancestorSearchSatisfied || matchesSearchQuery(episode.title, query);
  return searchSatisfied ? episode : null;
}

/**
 * Filters one Season (COL-104/COL-104 tracked-filter fix). The Tracked
 * filter gates the Season's *own* resolved Tracked value first and
 * independently of its descendants: if the Season itself doesn't resolve to
 * the selected filter, the whole branch is dropped even when an Episode
 * beneath it happens to match (AC2 grants search, not the Tracked filter,
 * an ancestor-context carve-out -- see `filterEpisode`/`filterSeriesNode`).
 * Only once that gate passes does the pre-existing search-context logic
 * run: a Season carries no title of its own to search against, so it only
 * counts as a direct match (unlocking every already-Tracked-filtered
 * Episode beneath it) once an ancestor Series title has already satisfied
 * the search. Otherwise it's kept only as context for a matching descendant
 * Episode, narrowed to just the Episodes that matched -- a search for one
 * Episode shouldn't drag its unrelated siblings back into view.
 */
function filterSeason(
  season: SeasonNode,
  query: string,
  trackedFilter: TrackedFilterValue,
  ancestorSearchSatisfied: boolean,
): SeasonNode | null {
  if (!matchesTrackedFilter(season.tracked, trackedFilter)) return null;
  const episodes = season.episodes
    .map((episode) => filterEpisode(episode, query, trackedFilter, ancestorSearchSatisfied))
    .filter((episode): episode is EpisodeNode => episode !== null);
  if (ancestorSearchSatisfied) {
    return { ...season, episodes };
  }
  return episodes.length > 0 ? { ...season, episodes } : null;
}

/**
 * Filters one Series (COL-104/COL-104 tracked-filter fix). The Tracked
 * filter gates the Series' *own* resolved Tracked value first and
 * independently of its descendants: if the Series itself doesn't resolve to
 * the selected filter, the whole subtree is dropped even when a Season or
 * Episode beneath it happens to match -- the Tracked filter gets no
 * ancestor-context carve-out (unlike search, see below). Only once that
 * gate passes does the pre-existing search logic run: kept if its own title
 * matches the search query, in which case every descendant Season/Episode
 * is shown too (each still independently gated by the Tracked filter) --
 * "search finds the Series, its whole subtree comes along" (AC1's
 * "descendants when a match is found" carve-out). Otherwise kept only when
 * a descendant Season/Episode matches on its own, so the Series row still
 * renders as the context a matching Season/Episode needs to have somewhere
 * to render under.
 */
function filterSeriesNode(
  series: SeriesNode,
  query: string,
  trackedFilter: TrackedFilterValue,
): SeriesNode | null {
  if (!matchesTrackedFilter(series.tracked, trackedFilter)) return null;
  const ownSearchMatch = matchesSearchQuery(series.title, query);
  const seasons = series.seasons
    .map((season) => filterSeason(season, query, trackedFilter, ownSearchMatch))
    .filter((season): season is SeasonNode => season !== null);
  if (ownSearchMatch) {
    return { ...series, seasons };
  }
  return seasons.length > 0 ? { ...series, seasons } : null;
}

/** Filters a Sonarr Library's whole Series list (COL-104); see `filterSeriesNode`. */
function filterSeriesList(
  series: SeriesNode[],
  query: string,
  trackedFilter: TrackedFilterValue,
): SeriesNode[] {
  return series
    .map((node) => filterSeriesNode(node, query, trackedFilter))
    .filter((node): node is SeriesNode => node !== null);
}

/**
 * Filters a Radarr Library's flat Movie list (COL-104): no hierarchy to
 * preserve context through, so a straight per-row AND of both filters.
 */
function filterMovies(movies: MovieNode[], query: string, trackedFilter: TrackedFilterValue): MovieNode[] {
  return movies.filter(
    (movie) => matchesSearchQuery(movie.title, query) && matchesTrackedFilter(movie.tracked, trackedFilter),
  );
}

/** Shared shape every row-level Tracked toggle needs from `LibraryPage` (COL-101). */
interface TrackedToggleProps {
  pendingNodeId: number | null;
  onToggle: (nodeType: LibraryNodeKind, nodeId: number, nextTracked: boolean) => void;
}

/**
 * Shared shape every row-level selection checkbox needs from `LibraryPage`
 * (COL-103): the selection set lives in `LibraryPage` (not the tree/table
 * component), so it survives a Series/Season row's own expand/collapse
 * toggling, which only touches `useExpandable`'s local state.
 */
interface SelectionProps {
  isSelected: (nodeType: LibraryNodeKind, nodeId: number) => boolean;
  onToggleSelect: (nodeType: LibraryNodeKind, nodeId: number) => void;
  selectionDisabled: boolean;
}

/** One row's Select checkbox (COL-103), shared by every node kind's row. */
function SelectionCheckbox({
  nodeType,
  nodeId,
  label,
  isSelected,
  onToggleSelect,
  selectionDisabled,
}: {
  nodeType: LibraryNodeKind;
  nodeId: number;
  label: string;
} & SelectionProps) {
  return (
    <input
      type="checkbox"
      className="library-tree-table__select"
      checked={isSelected(nodeType, nodeId)}
      disabled={selectionDisabled}
      onChange={() => onToggleSelect(nodeType, nodeId)}
      aria-label={label}
    />
  );
}

/**
 * The Sonarr Series > Season > Episode tree (COL-100): Series rows expand to
 * their Seasons, Season rows expand to their Episodes. Only Episode rows
 * carry `has_file` (per `collapsarr/library/routes.py`'s schema), so that's
 * the level the file-status badge/dimming applies to. Every row carries its
 * own Tracked toggle (COL-101): toggling a Series or Season row cascades
 * server-side to its descendants, reflected here once the caller's refetch
 * (triggered by `onToggle`) lands with their updated resolved values.
 *
 * `expandAll` (COL-104): while a search query or Tracked filter is active,
 * `LibraryPage` passes this as `true` so a filtered-in Season/Episode is
 * actually visible without the user separately clicking every ancestor's
 * expand toggle -- the whole point of showing an ancestor "for context" is
 * defeated if it's still collapsed. Local expand/collapse state is
 * preserved underneath and resumes once filters clear.
 */
function SeriesTree({
  series,
  pendingNodeId,
  onToggle,
  isSelected,
  onToggleSelect,
  selectionDisabled,
  expandAll,
}: { series: SeriesNode[]; expandAll: boolean } & TrackedToggleProps & SelectionProps) {
  const [expandedSeries, toggleSeries] = useExpandable();
  const [expandedSeasons, toggleSeason] = useExpandable();
  const selection: SelectionProps = { isSelected, onToggleSelect, selectionDisabled };

  return (
    <div className="panel library-tree-panel">
      <table className="library-tree-table">
        <thead>
          <tr>
            <th scope="col">Select</th>
            <th scope="col">Title</th>
            <th scope="col">File</th>
            <th scope="col">Tracked</th>
          </tr>
        </thead>
        <tbody>
          {series.map((seriesNode) => {
            const seriesOpen = expandAll || expandedSeries.has(seriesNode.id);
            return (
              <Fragment key={seriesNode.id}>
                <tr className="library-tree-table__row library-tree-table__row--series">
                  <td>
                    <SelectionCheckbox
                      nodeType="series"
                      nodeId={seriesNode.id}
                      label={`Select ${seriesNode.title}`}
                      {...selection}
                    />
                  </td>
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
                    const seasonOpen = expandAll || expandedSeasons.has(seasonNode.id);
                    return (
                      <Fragment key={seasonNode.id}>
                        <tr className="library-tree-table__row library-tree-table__row--season">
                          <td>
                            <SelectionCheckbox
                              nodeType="season"
                              nodeId={seasonNode.id}
                              label={`Select ${seriesNode.title} Season ${seasonNode.season_number}`}
                              {...selection}
                            />
                          </td>
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
                              <td>
                                <SelectionCheckbox
                                  nodeType="episode"
                                  nodeId={episode.id}
                                  label={`Select ${seriesNode.title} S${seasonNode.season_number}E${episode.episode_number} ${episode.title}`}
                                  {...selection}
                                />
                              </td>
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
function MovieTable({
  movies,
  pendingNodeId,
  onToggle,
  isSelected,
  onToggleSelect,
  selectionDisabled,
}: { movies: MovieNode[] } & TrackedToggleProps & SelectionProps) {
  return (
    <div className="panel library-tree-panel">
      <table className="library-tree-table">
        <thead>
          <tr>
            <th scope="col">Select</th>
            <th scope="col">Title</th>
            <th scope="col">File</th>
            <th scope="col">Tracked</th>
          </tr>
        </thead>
        <tbody>
          {movies.map((movie) => (
            <tr key={movie.id} className={fileNodeRowClassName(movie.has_file, "library-tree-table__row")}>
              <td>
                <SelectionCheckbox
                  nodeType="movie"
                  nodeId={movie.id}
                  label={`Select ${movie.title}`}
                  isSelected={isSelected}
                  onToggleSelect={onToggleSelect}
                  selectionDisabled={selectionDisabled}
                />
              </td>
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
 * The bulk-action toolbar (COL-103): appears once the cross-level selection
 * holds at least one reference, offering "Mark Tracked" / "Mark Not Tracked"
 * across the whole mixed-level selection in a single bulk-update request.
 * Renders nothing once the selection is empty, so it disappears the moment
 * the last checked row is unchecked or a successful apply clears it.
 */
function BulkActionToolbar({
  count,
  pending,
  onApply,
  onClear,
}: {
  count: number;
  pending: boolean;
  onApply: (tracked: boolean) => void;
  onClear: () => void;
}) {
  if (count === 0) return null;
  return (
    <div className="library-bulk-toolbar" role="toolbar" aria-label="Bulk Tracked actions">
      <span className="library-bulk-toolbar__count">
        {count} {count === 1 ? "row" : "rows"} selected
      </span>
      <button type="button" className="btn btn--primary btn--sm" disabled={pending} onClick={() => onApply(true)}>
        {pending ? "Updating…" : "Mark Tracked"}
      </button>
      <button
        type="button"
        className="btn btn--secondary btn--sm"
        disabled={pending}
        onClick={() => onApply(false)}
      >
        {pending ? "Updating…" : "Mark Not Tracked"}
      </button>
      <button
        type="button"
        className="library-bulk-toolbar__clear"
        disabled={pending}
        onClick={onClear}
      >
        Clear selection
      </button>
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
 * Every row also carries a Select checkbox (COL-103): the selection is a
 * `Set<string>` of `{node_type, node_id}` keys held here in `LibraryPage`
 * (not in `SeriesTree`/`MovieTable`), so it survives a branch's own
 * expand/collapse and can span any mix of Series/Season/Episode/Movie rows
 * across branches. Once the selection is non-empty, `BulkActionToolbar`
 * appears and its Mark Tracked / Mark Not Tracked actions send the whole
 * selection to the same bulk-update endpoint in one request -- the same
 * refetch-after-update pattern as the single-row toggle reflects the result
 * (including any cascade to a selected Series/Season's descendants) without
 * a manual refresh. A successful bulk apply clears the selection; a failed
 * one leaves it as-is so the user can retry.
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
 *
 * A title search box and a Tracked-status filter (COL-104) narrow which of
 * the already-fetched rows are visible -- both are purely client-side over
 * `treeState`'s tree, no separate endpoint/query param, since the whole
 * tree is already in hand once the page has loaded. They compose via AND
 * (`filterSeriesList`/`filterMovies`) and are independent of the COL-103
 * selection: `selected` is never touched when the filters change, so a
 * selected-but-now-filtered-out row's selection persists (it reappears
 * checked once the filter that hid it is cleared).
 */
export function LibraryPage() {
  const { instanceId: instanceIdParam } = useParams<{ instanceId: string }>();
  const instanceId = Number(instanceIdParam);

  const [treeState, setTreeState] = useState<TreeLoadState>({ status: "loading" });
  const [pendingNodeId, setPendingNodeId] = useState<number | null>(null);
  const [toggleError, setToggleError] = useState<string | null>(null);
  // Cross-level bulk selection (COL-103): a set of `selectionKey(nodeType,
  // nodeId)` strings, so it can hold any mix of Series/Season/Episode/Movie
  // references at once, independent of which branch each lives under.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkPending, setBulkPending] = useState(false);
  // Title search + Tracked filter (COL-104), composed via AND below.
  const [searchQuery, setSearchQuery] = useState("");
  const [trackedFilter, setTrackedFilter] = useState<TrackedFilterValue>("all");
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
    // A different instance's node ids aren't the same nodes -- drop any
    // selection made against the previous tree rather than carry over
    // references that no longer mean anything here.
    setSelected(new Set());
    // A search/filter scoped to the previous instance's titles isn't
    // meaningful here either (COL-104).
    setSearchQuery("");
    setTrackedFilter("all");
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

  function isSelected(nodeType: LibraryNodeKind, nodeId: number): boolean {
    return selected.has(selectionKey(nodeType, nodeId));
  }

  function handleToggleSelect(nodeType: LibraryNodeKind, nodeId: number) {
    const key = selectionKey(nodeType, nodeId);
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }

  function handleClearSelection() {
    setSelected(new Set());
  }

  async function handleBulkApply(nextTracked: boolean) {
    if (selected.size === 0) return;
    setToggleError(null);
    setBulkPending(true);
    try {
      const references: TrackedNodeReference[] = Array.from(selected).map(decodeSelectionKey);
      await bulkUpdateTracked(references, nextTracked);
      // Same refetch-after-update pattern as the single-row toggle: reflects
      // every selected node's new value plus any Series/Season cascade to
      // its descendants, without a manual page refresh.
      await loadTree();
      handleClearSelection();
    } catch (error: unknown) {
      setToggleError(error instanceof Error ? error.message : "Unknown error.");
    } finally {
      setBulkPending(false);
    }
  }

  const instance =
    instancesState.status === "ready"
      ? (instancesState.instances.find((candidate) => candidate.id === instanceId) ?? null)
      : null;

  // A filter is "active" once it could narrow anything, driving `SeriesTree`'s
  // `expandAll` (COL-104) -- a filtered-in Season/Episode should be visible
  // immediately, not hidden behind an ancestor's default-collapsed state.
  const filtersActive = searchQuery.trim() !== "" || trackedFilter !== "all";

  const filteredSeries = useMemo(() => {
    if (treeState.status !== "ready" || isMovieTree(treeState.tree)) return [];
    return filterSeriesList(treeState.tree.series, searchQuery, trackedFilter);
  }, [treeState, searchQuery, trackedFilter]);

  const filteredMovies = useMemo(() => {
    if (treeState.status !== "ready" || !isMovieTree(treeState.tree)) return [];
    return filterMovies(treeState.tree.movies, searchQuery, trackedFilter);
  }, [treeState, searchQuery, trackedFilter]);

  const hasOriginalNodes =
    treeState.status === "ready" &&
    (isMovieTree(treeState.tree) ? treeState.tree.movies.length > 0 : treeState.tree.series.length > 0);

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

      {hasOriginalNodes && (
        <div className="library-filters">
          <input
            type="search"
            className="library-filters__input"
            placeholder="Search titles…"
            aria-label="Search titles"
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
          />
          <select
            className="library-filters__select"
            aria-label="Tracked filter"
            value={trackedFilter}
            onChange={(event) => setTrackedFilter(event.target.value as TrackedFilterValue)}
          >
            <option value="all">All</option>
            <option value="tracked">Tracked</option>
            <option value="not-tracked">Not Tracked</option>
          </select>
        </div>
      )}

      {toggleError && <p className="form-error">Couldn&apos;t update Tracked: {toggleError}</p>}

      <BulkActionToolbar
        count={selected.size}
        pending={bulkPending}
        onApply={(nextTracked) => void handleBulkApply(nextTracked)}
        onClear={handleClearSelection}
      />

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
        ) : filteredSeries.length === 0 ? (
          <div className="panel panel--empty">
            <span className="panel__icon" aria-hidden>
              <LibraryIcon width={28} height={28} />
            </span>
            <p className="panel__message">No series match your search or filter.</p>
          </div>
        ) : (
          <SeriesTree
            series={filteredSeries}
            pendingNodeId={pendingNodeId}
            onToggle={handleToggle}
            isSelected={isSelected}
            onToggleSelect={handleToggleSelect}
            selectionDisabled={bulkPending}
            expandAll={filtersActive}
          />
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
        ) : filteredMovies.length === 0 ? (
          <div className="panel panel--empty">
            <span className="panel__icon" aria-hidden>
              <LibraryIcon width={28} height={28} />
            </span>
            <p className="panel__message">No movies match your search or filter.</p>
          </div>
        ) : (
          <MovieTable
            movies={filteredMovies}
            pendingNodeId={pendingNodeId}
            onToggle={handleToggle}
            isSelected={isSelected}
            onToggleSelect={handleToggleSelect}
            selectionDisabled={bulkPending}
          />
        ))}
    </section>
  );
}
