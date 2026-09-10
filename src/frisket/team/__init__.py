"""Open team edition: identity, membership, RBAC and project metadata.

`frisket.team` is the OPEN half of the control plane (Apache-2.0, shipped in
the public `frisket` package). It owns the identity schema and its bootstrap.

Boundary rule: an external commerce composition may import
`frisket.team`; `frisket.team` must NEVER import it back. Commerce
state — credits, quotas, funding, spend caps, signup gating — lives entirely
outside this tree and references identity rows by id.
"""
