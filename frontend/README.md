# Collapsarr Web UI

The Collapsarr frontend: a dark, sidebar-nav app shell in the Sonarr / Radarr /
Bazarr tradition, with the Collapsarr purple accent (`#8B5CF6`).

## Stack

| Concern    | Choice                              | Why |
| ---------- | ----------------------------------- | --- |
| Framework  | **React 18 + TypeScript**           | Ubiquitous, matches the *arr ecosystem (Sonarr/Radarr/Bazarr UIs are all React), easy for contributors. |
| Build tool | **Vite 5**                          | Fast dev server, zero-config TS, emits a small self-contained static bundle. |
| Routing    | **react-router-dom 6**              | Standard client-side routing; the nav model is a single array in `src/routes/router.tsx`. |
| Styling    | **Plain CSS + CSS custom properties** | No component library needed for a small self-hosted app. Design tokens live in `src/styles/theme.css`; the shell is dark-only by design. |
| Tests      | **Vitest + Testing Library (jsdom)** | Lightweight, shares Vite config. |
| Lint       | **ESLint (flat config) + typescript-eslint** | — |

These were engineering calls made for the app-shell ticket (COL-30); nothing
here dictates future feature choices.

## Commands

```bash
npm install       # install dependencies
npm run dev       # dev server at http://localhost:5173
npm run build     # type-agnostic production build -> ./dist
npm run typecheck # tsc --noEmit
npm run lint      # eslint
npm run test      # vitest run (app-shell smoke test)
```

## Routes

- `/wanted` (default) — files still missing an enabled downmix target (COL-31)
- `/activity` — downmix job history (COL-32)
- `/settings` — instances, path mappings, downmix targets, Connect (COL-33)

All three are placeholder views for now; the real screens land in the tickets noted.

## How this ships (backend integration)

The backend is FastAPI (`collapsarr/`). Per the v1 design doc, frontend assets
are bundled into the PyPI wheel (Bazarr's approach) and served by the backend.

`npm run build` emits a self-contained static bundle into `frontend/dist/`
(`base: "./"` keeps asset URLs relative, so it works under any mount path). A
later ticket wires FastAPI static-file serving and folds `dist/` into the wheel
build; **this ticket only makes the frontend buildable on its own** — nothing in
the backend is touched yet.
