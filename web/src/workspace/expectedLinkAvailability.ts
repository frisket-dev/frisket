import type { EvaluationLink } from '../api/evaluation';

/** Availability comes from the expected-link response, independently per link. */
export function isExpectedLinkAvailable(link: EvaluationLink): boolean {
  return link.available !== false;
}
export function expectedLinkUnavailableReason(link: EvaluationLink): string {
  return link.unavailable_reason
    || 'This expected answers link is unavailable.';
}
