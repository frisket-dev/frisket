# frisket web

Frontend for frisket: React + TypeScript + Vite, with
[Glide Data Grid](https://github.com/glideapps/glide-data-grid) as the
virtualized multimodal grid.

## Run

```sh
npm install
npm run dev      # http://localhost:5173
npm run build    # type-check + production bundle
```

## Data layer

The UI consumes the `FrisketApi` interface through `src/api/open.ts`. Its live
implementation is composed in `src/api/real.ts` from typed API adapters.
Contract-backed requests use `src/api/httpContract.ts`; narrowly scoped browser
integrations such as authentication, blob text, and Arrow map data live in
`src/api/raw/`.

## Layout

- `src/api/` — domain types, ports, contract adapters, and public composition
- `src/grid/` — Glide wrapper, custom media cell renderer, paged row cache
- `src/components/` — workbench panels, forms, drawers, status, and review surfaces
- `src/actions/` — action catalog models and v1 authoring contracts

## Notes

- React is pinned to 18.x (`@glideapps/glide-data-grid@6.0.3` peers cap at 18);
  `marked` is pinned to 4.x for the same reason.
- Playwright uses stable, behavior-oriented `data-testid` hooks for workbench,
  action, review, history, progress, and detail surfaces.
- The review queue is an overlay route on the current project path (a/r/e accept/reject/edit,
  j/k navigate, esc close).
