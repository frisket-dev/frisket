"""Open core security primitives (crypto, not tenant/billing policy).

Lives outside the private edition on purpose: core modules (project store,
provider-key settings, plugin secrets, notifications) need envelope
encryption without depending on the private-hosted control plane. The full
threat model and envelope contract live in `frisket.security.secrets`.
"""

from __future__ import annotations
