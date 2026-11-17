# Consent binds the facts of one estimate, not a boolean

Consent is a hash minted once, by `mint_confirmation_hash` (`src/frisket/engine/runner/confirmation_context.py`), over the action family, its scope, the facts resolved at the gate and the estimate; a paid or external run proceeds only when the hash the caller echoes equals the one minted for that run (`src/frisket/execution/attempt_authority.py`). Those facts travel by value from the estimate to dispatch.

**Considered:** a `confirmed` flag. Fewer parameters, but a yes given for one estimate can be replayed against a different estimate, scope or action.

**Consequences:** more parameters and hashed fields on every path to a paid or external run; any change to what was estimated voids the consent and asks again.
