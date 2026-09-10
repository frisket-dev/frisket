"""Registered-domain (eTLD+1) computation backed by the FULL vendored Public
Suffix List.

The matcher registry accepts UNTRUSTED plugin-declared domains, so the minimal
hand-curated snapshot the registry core once shipped
(``PSL_SNAPSHOT_VERSION == "2026-07-07-core-minimal"``, sufficient only for the
handful of first-party domains) is retired here in favour of the complete
Mozilla Public Suffix List, vendored as a checked-in data file. A plugin can now
declare ``foo.github.io``-style domains and the host correctly refuses to
collapse them to their public suffix.

VENDORING (network at VENDOR time only, never at runtime):

- Source URL: https://publicsuffix.org/list/public_suffix_list.dat (the ONLY
  supported retrieval URL per the list header).
- Vendored artifact: ``src/frisket/data/public_suffix_list.dat`` (checked in).
- Retrieved: 2026-07-07. Upstream ``// VERSION: 2026-07-06_09-10-11_UTC``,
  ``// COMMIT: 5ae2220ad86eab1364821329ff58d28828bddb19``.
- Refresh command (re-fetch + re-pin, run from the repo root)::

      uv run python -m frisket.features.url_classification.psl --refresh

  This overwrites the vendored ``.dat`` from the source URL and prints the new
  upstream VERSION/COMMIT to fold into ``PSL_SNAPSHOT_VERSION`` and the header
  above. Bump the snapshot version and this artifact together via a dated task;
  never hand-edit the eTLD+1 boundary.

Runtime never touches the network: the ``.dat`` is parsed once from the vendored
file (cached module-level) into exact / wildcard / exception rule sets and the
standard PSL algorithm (https://publicsuffix.org/list/) resolves the registered
domain. ``matchers.host_matches_domain`` still does the cheap label-boundary
suffix match for candidate selection; ``registered_domain`` here is used for host
NORMALIZATION / reporting and to keep untrusted-domain collapse honest.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

# Snapshot version. Pins the vendored list's provenance; bump together with the
# .dat artifact on every refresh. Encodes the upstream VERSION date so a drift is
# visible in the exported matcher snapshot's ``psl_snapshot_version`` field.
PSL_SNAPSHOT_VERSION = "2026-07-07-psl-full-2026-07-06"

PSL_SOURCE_URL = "https://publicsuffix.org/list/public_suffix_list.dat"
PSL_RETRIEVED = "2026-07-07"
PSL_UPSTREAM_VERSION = "2026-07-06_09-10-11_UTC"
PSL_UPSTREAM_COMMIT = "5ae2220ad86eab1364821329ff58d28828bddb19"

_PSL_DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "public_suffix_list.dat"


@lru_cache(maxsize=1)
def _psl_rules() -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Parse the vendored list into (exact, wildcard, exception) rule sets.

    - exact: normal suffix rules (``com``, ``co.uk``, ``github.io``).
    - wildcard: the labels to the RIGHT of a leading ``*`` (``*.ck`` -> ``ck``,
      ``*.kawasaki.jp`` -> ``kawasaki.jp``).
    - exception: the suffix from a ``!`` rule minus the leading ``!``
      (``!www.ck`` -> ``www.ck``).
    """
    exact: set[str] = set()
    wildcard: set[str] = set()
    exception: set[str] = set()
    for raw in _PSL_DATA_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        # Rules are IDNA/ASCII in the vendored list; lower-case for matching.
        rule = line.lower()
        if rule.startswith("!"):
            exception.add(rule[1:])
        elif rule.startswith("*."):
            wildcard.add(rule[2:])
        elif rule == "*":
            # A bare "*" wildcard (no such rule ships today, but be defensive):
            # treat as the default rule, already handled below.
            continue
        else:
            exact.add(rule)
    return frozenset(exact), frozenset(wildcard), frozenset(exception)


def _public_suffix_label_count(labels: tuple[str, ...]) -> int:
    """Number of trailing labels that form the public suffix of ``labels``.

    Standard PSL algorithm: exception rules win outright (their public suffix is
    the rule minus its leftmost label); otherwise the longest matching exact or
    wildcard rule wins; if nothing matches, the default rule makes the TLD (one
    label) the public suffix.
    """
    exact, wildcard, exception = _psl_rules()
    n = len(labels)

    # Exception rules take priority over any other match.
    for i in range(n):
        candidate = ".".join(labels[i:])
        if candidate in exception:
            return n - i - 1  # drop the leftmost label of the exception rule

    best = 1  # default rule: the rightmost label is a public suffix
    for i in range(n):
        candidate = ".".join(labels[i:])
        length = n - i
        if candidate in exact and length > best:
            best = length
        # Wildcard: the label at position i is the "*", the remainder must be a
        # registered wildcard stem (labels[i+1:]).
        stem = ".".join(labels[i + 1 :])
        if stem and stem in wildcard and length > best:
            best = length
    return best


def registered_domain(host: str) -> str:
    """Return the registered domain (eTLD+1) of ``host``.

    ``music.youtube.com`` -> ``youtube.com``; ``foo.github.io`` ->
    ``foo.github.io`` (github.io is a public suffix); ``a.b.ck`` -> ``a.b.ck``
    (``*.ck`` wildcard); ``www.ck`` -> ``www.ck`` (``!www.ck`` exception).
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host or "." not in host:
        return host
    labels = tuple(host.split("."))
    suffix_len = _public_suffix_label_count(labels)
    take = suffix_len + 1
    if take > len(labels):
        # host IS a public suffix (or shorter) — return it unchanged.
        return host
    return ".".join(labels[-take:])


def _refresh() -> int:
    """Re-fetch the vendored list from the source URL (vendor-time only)."""
    import urllib.request

    with urllib.request.urlopen(PSL_SOURCE_URL, timeout=30) as response:  # noqa: S310
        data = response.read()
    _PSL_DATA_PATH.write_bytes(data)
    text = data.decode("utf-8", errors="replace")
    version = commit = "?"
    for line in text.splitlines():
        if line.startswith("// VERSION:"):
            version = line.split(":", 1)[1].strip()
        elif line.startswith("// COMMIT:"):
            commit = line.split(":", 1)[1].strip()
    print(f"refreshed {_PSL_DATA_PATH}")
    print(f"upstream VERSION: {version}")
    print(f"upstream COMMIT: {commit}")
    print("-> bump PSL_SNAPSHOT_VERSION and the module header to match")
    return 0


if __name__ == "__main__":
    if "--refresh" in sys.argv[1:]:
        raise SystemExit(_refresh())
    print(f"PSL_SNAPSHOT_VERSION={PSL_SNAPSHOT_VERSION}")
    print(f"source={PSL_SOURCE_URL} retrieved={PSL_RETRIEVED}")
    _exact, _wildcard, _exception = _psl_rules()
    print(
        f"rules: exact={len(_exact)} wildcard={len(_wildcard)} "
        f"exception={len(_exception)}"
    )
