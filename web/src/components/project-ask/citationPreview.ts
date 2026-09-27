import type { AskCitation } from '../../api/projectQA';

export function hasCitationPreview(citation: AskCitation): boolean {
  return citation.target?.kind === 'web' || citation.status === 'unavailable' || citation.status === 'changed';
}
