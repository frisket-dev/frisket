# Gate baselines and allowlists only shrink

Each ratchet in `scripts/ci/` keeps a baseline or allowlist whose `--update` lowers counts and drops entries but never raises or adds, and a gate fails on an entry that no longer offends. Nothing regresses silently.

**Considered:** regenerating baselines when a change needs it. No friction, but each regeneration can absorb a regression unseen.

**Consequences:** moving or renaming a file or function means editing its baseline key by hand; a change that would raise a count is restructured instead.
