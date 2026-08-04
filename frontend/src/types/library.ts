/**
 * Types mirroring the `GET /api/library/instances/{id}/tree` response
 * (COL-98/COL-99, `collapsarr/library/routes.py`) -- kept in sync by hand
 * since there's no shared schema generation yet.
 *
 * A Sonarr instance returns `LibraryTreeResponse` (Series > Season >
 * Episode, each node carrying its *resolved* Tracked value); a Radarr
 * instance returns `MovieLibraryTreeResponse` (a flat Movie list). Which
 * shape comes back is decided server-side by the instance's configured
 * type, not anything the caller requests, so the frontend discriminates the
 * response body itself -- see `isMovieTree`.
 */

/** A leaf Episode node, matching `collapsarr.library.routes.EpisodeNode`. */
export interface EpisodeNode {
  id: number;
  kind: "episode";
  sonarr_episode_id: number;
  season_number: number;
  episode_number: number;
  title: string;
  has_file: boolean;
  tracked: boolean;
}

/** A Season node with its Episode children, matching `...routes.SeasonNode`. */
export interface SeasonNode {
  id: number;
  kind: "season";
  season_number: number;
  tracked: boolean;
  episodes: EpisodeNode[];
}

/** A Series node with its Season children, matching `...routes.SeriesNode`. */
export interface SeriesNode {
  id: number;
  kind: "series";
  sonarr_series_id: number;
  title: string;
  tracked: boolean;
  seasons: SeasonNode[];
}

/** A Sonarr instance's full Library tree, matching `...routes.LibraryTreeResponse`. */
export interface LibraryTreeResponse {
  instance_id: number;
  series: SeriesNode[];
}

/** A leaf Movie node (COL-99), matching `...routes.MovieNode`. */
export interface MovieNode {
  id: number;
  kind: "movie";
  radarr_movie_id: number;
  title: string;
  has_file: boolean;
  tracked: boolean;
}

/** A Radarr instance's flat Movie list (COL-99), matching `...routes.MovieLibraryTreeResponse`. */
export interface MovieLibraryTreeResponse {
  instance_id: number;
  movies: MovieNode[];
}

/** The union the tree endpoint returns; narrowed by `isMovieTree`. */
export type LibraryTree = LibraryTreeResponse | MovieLibraryTreeResponse;

/** Discriminates the flat Radarr Movie shape from the Sonarr Series tree shape. */
export function isMovieTree(tree: LibraryTree): tree is MovieLibraryTreeResponse {
  return "movies" in tree;
}
