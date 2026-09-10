// Engine-tier badge: ONE small text badge per engine naming where it runs —
// local / sidecar / hosted — rendered from the catalog's three-tier vocabulary
// (api/types.ts EngineTier; backend tier tables in
// src/frisket/contracts/actions/schemas/_engines.py). Reuses the StatusChip
// primitive rather than inventing a new pill; all three tiers share the SAME
// quiet neutral tone — the tier is carried by the text (never color-only, and no
// tone editorializing about which tier is "good").
//
// Honest-copy rule: 'sidecar' is trusted BY CONFIGURATION — its URL is
// operator-set and may point at another machine, so the tooltip says "the
// operator's sidecar service", never "local"/"this machine".

import { StatusChip } from './PanelPrimitives';
import { engineTierLabel } from '../actions/engineCatalog';
import type { EngineTier } from '../api/types';

const TIER_TITLES: Record<EngineTier, string> = {
  local: 'Runs inside frisket itself — nothing leaves the frisket host.',
  sidecar:
    "Runs on the operator's configured sidecar service, which may be a different machine.",
  hosted: 'Sent to an external hosted service outside your infrastructure.',
};

export function EngineTierBadge({ tier, testId }: { tier: EngineTier; testId?: string }) {
  return (
    <StatusChip
      tone="neutral"
      size="sm"
      className="engine-tier-badge"
      testId={testId}
      data-tier={tier}
      title={TIER_TITLES[tier]}
    >
      {engineTierLabel(tier).toLowerCase()}
    </StatusChip>
  );
}
