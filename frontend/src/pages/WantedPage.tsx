import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { fetchWantedList } from "../api/wanted";
import { WantedIcon } from "../components/icons";
import type { WantedFile } from "../types/wanted";

const TARGET_LABEL: Record<string, string> = {
  stereo: "Stereo",
  "2.1": "2.1",
  "5.1": "5.1",
};

/** Best-effort display title from a file path: last segment, minus extension. */
function titleFromPath(filePath: string): string {
  const base = filePath.split(/[/\\]/).pop() || filePath;
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(0, dot) : base;
}

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; files: WantedFile[] };

/**
 * The Wanted view (COL-31): every tracked file still missing at least one
 * enabled downmix target, sourced live from `GET /api/wanted` (COL-28).
 */
export function WantedPage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;

    fetchWantedList()
      .then((files) => {
        if (!cancelled) {
          setState({ status: "ready", files });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({
            status: "error",
            message: error instanceof Error ? error.message : "Unknown error.",
          });
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <section className="view">
      <header className="view__header">
        <h1 className="view__title">Wanted</h1>
        <p className="view__summary">Monitored files still missing an enabled downmix target.</p>
      </header>

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading wanted files…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <WantedIcon width={28} height={28} />
          </span>
          <p className="panel__message">Couldn&apos;t load the wanted list: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && state.files.length === 0 && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <WantedIcon width={28} height={28} />
          </span>
          <p className="panel__message">
            Nothing wanted right now. Every tracked file already has all enabled downmix targets.
          </p>
        </div>
      )}

      {state.status === "ready" && state.files.length > 0 && (
        <div className="panel wanted-panel">
          <table className="wanted-table">
            <thead>
              <tr>
                <th scope="col">Title</th>
                <th scope="col">Path</th>
                <th scope="col">Missing targets</th>
              </tr>
            </thead>
            <tbody>
              {state.files.map((file) => (
                <tr key={file.id}>
                  <td className="wanted-table__title">
                    <Link to={`/wanted/${file.id}`}>{titleFromPath(file.file_path)}</Link>
                  </td>
                  <td className="wanted-table__path">{file.file_path}</td>
                  <td>
                    {file.missing_targets.length === 0 ? (
                      <span className="wanted-table__none">—</span>
                    ) : (
                      <ul className="wanted-table__targets">
                        {file.missing_targets.map((missing) => (
                          <li key={`${missing.language}-${missing.target}`} className="panel__tag">
                            {missing.language} · {TARGET_LABEL[missing.target] ?? missing.target}
                          </li>
                        ))}
                      </ul>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
