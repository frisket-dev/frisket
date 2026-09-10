## What & why

<!-- One or two sentences: what changed, and why. Keep the PR scoped to one
     logical change -- see CONTRIBUTING.md. -->

## Checklist

- [ ] Tests pass locally: `uv run pytest tests -q`
- [ ] Lint/format are clean: `uv run ruff check src tests` and
      `uv run ruff format --check src tests`
- [ ] New/changed behavior has a focused test that proves it
- [ ] `frisket doctor` still runs clean, if this touches install/deploy,
      diagnostics, optional extras, or startup paths
- [ ] `web` changes build: `npm run build --prefix web` (and `npm ci` first
      if `package-lock.json` moved)
- [ ] No secrets, no copyrighted media, and only public-domain or
      redistribution-clean fixture data
- [ ] README/help text updated if this changes user-facing behavior or install steps
- [ ] For architecture or high-risk migrations: review ownership, removal and
      compatibility plans, and invariant coverage; record material decisions
      in the PR or a linked decision record

## Testing

<!-- How did you verify this? Commands run, screenshots for UI changes, etc. -->

## Notes for reviewers

<!-- Anything out of scope on purpose, follow-ups filed, or context a
     reviewer needs that isn't obvious from the diff. -->
