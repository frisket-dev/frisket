import type { EvaluationLink } from '../api/evaluation';
import { Button } from '../components/Button';
import { PanelNotice } from '../components/PanelPrimitives';
import { expectedLinkUnavailableReason } from './expectedLinkAvailability';

export function UnavailableExpectedLinks({ links, onRemove }: { links: EvaluationLink[]; onRemove(id: number): void }) {
  return <>{links.map(link => <PanelNotice key={link.id} tone="warning">
    <span>Expected answers · {link.expected_sheet_name}: {expectedLinkUnavailableReason(link)} </span>
    <Button type="button" onClick={() => onRemove(link.id)}>Remove link</Button>
  </PanelNotice>)}</>;
}
