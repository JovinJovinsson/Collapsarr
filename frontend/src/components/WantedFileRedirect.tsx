import { Navigate, useParams } from "react-router-dom";

/**
 * Redirects the old per-file detail path (`/wanted/:fileId`) to its COL-203
 * replacement (`/files/:fileId`), preserving the id. `<Navigate>` can't
 * interpolate a route param directly, so this small wrapper reads it via
 * `useParams` first -- kept as a permanent redirect (not a temporary one to
 * delete later) since any bookmarked/shared old-style link should keep
 * working indefinitely. Lives in its own file (rather than inline in
 * `routes/router.tsx`) so that file keeps exporting only route config, not a
 * component -- `router.tsx` mixing the two trips `react-refresh/only-export-components`.
 */
export function WantedFileRedirect() {
  const { fileId } = useParams<{ fileId: string }>();
  return <Navigate to={`/files/${fileId}`} replace />;
}
