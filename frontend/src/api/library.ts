import type {
  BulkTrackedUpdateRequest,
  BulkTrackedUpdateResponse,
  LibraryNodeKind,
  LibraryTree,
  TrackedNodeReference,
} from "../types/library";
import { apiErrorMessage, apiFetch } from "./client";

const LAST_VISITED_STORAGE_KEY = "collapsarr.libraries.lastInstanceId";
const JSON_HEADERS = { "Content-Type": "application/json" };

/**
 * Fetches an instance's Library tree
 * (`GET /api/library/instances/{id}/tree`, COL-98/COL-99): the
 * Series > Season > Episode tree for a Sonarr instance, or the flat Movie
 * list for a Radarr one. See `types/library.ts#isMovieTree` for telling the
 * two shapes apart.
 */
export async function fetchLibraryTree(instanceId: number): Promise<LibraryTree> {
  const response = await apiFetch(`/api/library/instances/${instanceId}/tree`);
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load library (${response.status})`));
  }
  return (await response.json()) as LibraryTree;
}

/**
 * Reads the last-visited Libraries instance id from localStorage (COL-100),
 * or `null` if none is recorded yet or storage is unavailable. Backs
 * `LibrariesIndexPage`'s "clicking Libraries itself goes to the
 * last-visited instance, falling back to the first configured one" redirect.
 */
export function readLastVisitedLibraryInstanceId(): number | null {
  try {
    const raw = globalThis.localStorage?.getItem(LAST_VISITED_STORAGE_KEY);
    if (!raw) return null;
    const parsed = Number(raw);
    return Number.isInteger(parsed) ? parsed : null;
  } catch {
    // Storage can throw in locked-down environments -- treat as "unrecorded".
    return null;
  }
}

/**
 * Persists the currently-viewed Libraries instance id (COL-100), so a later
 * bare "Libraries" click returns here instead of falling back to the first
 * configured instance.
 */
export function rememberVisitedLibraryInstance(instanceId: number): void {
  try {
    globalThis.localStorage?.setItem(LAST_VISITED_STORAGE_KEY, String(instanceId));
  } catch {
    // Best-effort; nothing sensible to do if storage is unavailable.
  }
}

/**
 * Shared `POST /api/library/tracked` call backing both `updateTracked`
 * (single reference) and `bulkUpdateTracked` (COL-103, a mixed-level
 * selection) -- the endpoint always takes a list of references, so a single
 * toggle is just the one-element case of a bulk update.
 */
async function postTrackedUpdate(
  references: TrackedNodeReference[],
  tracked: boolean,
): Promise<BulkTrackedUpdateResponse> {
  const body: BulkTrackedUpdateRequest = { references, tracked };
  const response = await apiFetch("/api/library/tracked", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to update Tracked (${response.status})`));
  }
  return (await response.json()) as BulkTrackedUpdateResponse;
}

/**
 * Sets Tracked on a single Library node (`POST /api/library/tracked`, COL-101),
 * cascading to its descendants server-side when it's a Series/Season
 * reference. Callers re-fetch the tree (`fetchLibraryTree`) afterwards to
 * reflect any cascaded descendant rows -- this bulk endpoint's response only
 * reports the directly-referenced node, not the full cascaded set.
 */
export async function updateTracked(
  nodeType: LibraryNodeKind,
  nodeId: number,
  tracked: boolean,
): Promise<BulkTrackedUpdateResponse> {
  return postTrackedUpdate([{ node_type: nodeType, node_id: nodeId }], tracked);
}

/**
 * Sets Tracked on an arbitrary mixed-level selection of Library nodes in one
 * request (COL-103, e.g. a Series from one branch plus an Episode from
 * another) -- the bulk-action toolbar's counterpart to `updateTracked`'s
 * single-reference form. Same cascade/refetch contract: a Series/Season
 * reference cascades to its descendants server-side, and the caller
 * re-fetches the tree to reflect the full resulting state.
 */
export async function bulkUpdateTracked(
  references: TrackedNodeReference[],
  tracked: boolean,
): Promise<BulkTrackedUpdateResponse> {
  return postTrackedUpdate(references, tracked);
}
