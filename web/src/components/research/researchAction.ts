/** Actions whose admitted execution records search/page activity. */
export function isResearchAction(kind: string | null | undefined): boolean {
  return kind === 'research.answer' || kind === 'research.extract' || kind === 'research.web_search';
}
