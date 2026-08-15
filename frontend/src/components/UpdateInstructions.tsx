import type { InstallMethod } from "../types/system";

/** The Docker Hub repository the release pipeline publishes to (`.github/workflows/release.yml`'s `IMAGE_NAME`). */
const DOCKER_IMAGE = "odxnsson/collapsarr";

/** GitHub Releases page, source of the native (PyInstaller) archives (`StatusPage`'s `SOURCE_URL` origin). */
const RELEASES_URL = "https://github.com/JovinJovinsson/Collapsarr/releases";

/**
 * Derives the Docker tag to pull from the latest known release tag.
 *
 * The release pipeline (`.github/workflows/release.yml`) publishes Docker
 * tags *without* the `v` prefix GitHub release tags carry (e.g. release tag
 * `v1.2.3` -> Docker tag `1.2.3`), so this strips a leading `v` rather than
 * passing `latestVersion` through verbatim. Falls back to `latest` when no
 * release has been fetched yet (`latestVersion` is `null`).
 */
function dockerTagFor(latestVersion: string | null): string {
  if (!latestVersion) return "latest";
  return latestVersion.replace(/^v/, "");
}

/**
 * The Updates page's static "How to update" block (COL-90, three-way switch
 * added in COL-219 for `"native"`; extracted from `UpdatesPage` into its own
 * subcomponent by COL-228, absorbing COL-91's proposed extraction of this
 * exact block).
 *
 * Switches on `installMethod` (`"docker"` | `"pipx"` | `"native"`, formerly
 * the `is_docker` boolean, COL-215): `docker pull`/recreate for `"docker"`,
 * `pipx upgrade`/`pip install --upgrade` for `"pipx"`, and a
 * download-the-archive-and-replace-the-install-folder walkthrough for
 * `"native"` (PyInstaller build, COL-216+) -- the native branch also calls
 * out that the database/config are safe because they live in the OS
 * user-data directory (`platformdirs.user_data_dir("collapsarr")`,
 * `collapsarr/system/info.py`'s `data_dir`), not inside the install folder
 * being replaced. See `docs/adr/0001-update-check-detect-notify-only.md` for
 * why detection is backend-only and why no code path here executes any of
 * these commands itself -- purely informational, same as the docker/pipx
 * branches.
 *
 * `UpdatesPage` (COL-228) renders this only when Self-Update itself has
 * nothing to offer for the current state -- Docker (an image cannot
 * self-replace), or a `native`/`pipx` install with no applicable
 * stable-channel build to apply -- falling back to these static,
 * manually-actioned instructions instead of the "Update Now" action. See
 * `CONTEXT.md`'s **Self-Update** entry.
 */
export function UpdateInstructions({
  installMethod,
  latestVersion,
}: {
  installMethod: InstallMethod;
  latestVersion: string | null;
}) {
  return (
    <div className="update-panel__instructions">
      <h2 className="update-panel__instructions-title">How to update</h2>
      {installMethod === "docker" && (
        <ol className="update-panel__instructions-steps">
          <li>
            Pull the new image:
            <pre className="update-panel__command">
              <code>
                docker pull {DOCKER_IMAGE}:{dockerTagFor(latestVersion)}
              </code>
            </pre>
          </li>
          <li>
            Recreate the container so it picks up the freshly pulled image:
            <pre className="update-panel__command">
              <code>docker compose up -d</code>
            </pre>
            (or, without Compose: <code>docker stop collapsarr &amp;&amp; docker rm collapsarr</code>,
            then re-run your <code>docker run</code> command.)
          </li>
        </ol>
      )}
      {installMethod === "pipx" && (
        <ol className="update-panel__instructions-steps">
          <li>
            Using pipx:
            <pre className="update-panel__command">
              <code>pipx upgrade collapsarr</code>
            </pre>
          </li>
          <li>
            Or, using pip directly:
            <pre className="update-panel__command">
              <code>pip install --upgrade collapsarr</code>
            </pre>
          </li>
        </ol>
      )}
      {installMethod === "native" && (
        <>
          <ol className="update-panel__instructions-steps">
            <li>
              Download the new archive for your platform from the{" "}
              <a href={RELEASES_URL} target="_blank" rel="noreferrer">
                release page
              </a>
              .
            </li>
            <li>Replace the install folder with the contents of the new archive.</li>
            <li>Restart Collapsarr.</li>
          </ol>
          <p className="update-panel__instructions-note">
            Your database and settings are safe: they&apos;re stored in your OS user-data
            directory, not inside the install folder you&apos;re replacing.
          </p>
        </>
      )}
    </div>
  );
}
