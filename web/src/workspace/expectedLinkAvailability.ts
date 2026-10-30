import type { EvaluationLink } from '../api/evaluation';

/** Older servers omit availability; existing links remain available by default. */
export function isExpectedLinkAvailable(link: EvaluationLink): boolean {
  return (link as EvaluationLink & { available?: boolean }).available !== false;
}
export function expectedLinkUnavailableReason(link: EvaluationLink): string {
  return (link as EvaluationLink & { unavailable_reason?: string | null }).unavailable_reason
    || 'This expected answers link is unavailable.';
}
