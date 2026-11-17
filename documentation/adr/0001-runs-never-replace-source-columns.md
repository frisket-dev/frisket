# A run never replaces a source column

The map runner refuses an output that resolves to an existing source column, whatever the request says about replacing; only a generated column is reused. The check sits in `prepare_validated` (`src/frisket/engine/runner/preparation.py`), where every map run resolves its outputs, so no caller can skip it.

**Considered:** a check in each caller before the run. A caller that skips it lets a run write over original data.

**Consequences:** an output named like a source column fails and asks for a rename.
