import { Table2 } from 'lucide-react';
import { navigate } from '../routes';
import { useInstanceIdentity } from '../instanceIdentity';

/** Instance wordmark — clicking it returns to the project picker. Shows the
 *  operator-configured instance display name (onboard-instance-identity-v1)
 *  when set, otherwise the generic "frisket" default. */
export function BrandLink({ size = 16 }: { size?: number }) {
  const instance = useInstanceIdentity();
  return (
    <button
      type="button"
      className="brand-link"
      title="All projects"
      data-testid="brand-home"
      onClick={() => navigate({ kind: 'picker' })}
    >
      <span className="brand-tile"><Table2 size={size - 2} strokeWidth={2.2} /></span>
      <span className="brand-name">{instance.display_name}</span>
    </button>
  );
}
