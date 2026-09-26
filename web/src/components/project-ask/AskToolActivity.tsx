import type { AskEvent } from '../../api/projectQA';

const labels: Record<string, string> = {
  inspect_sheets: 'Checking sources', read_rows: 'Reading records', query_rows: 'Checking matching records',
  analytics: 'Calculating across matching records', search_cells: 'Searching project content',
  open_source: 'Reading a source', find_in_source: 'Finding text in a source', list_sources: 'Checking earlier sources',
  search_actions: 'Finding an action', describe_action: 'Checking action settings',
  propose_action: 'Preparing an action suggestion', search_web: 'Searching the web', open_web_page: 'Reading a web page',
};
function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
function qualityFacts(value: unknown): string[] {
  return Object.entries(record(value) ?? {}).flatMap(([column, raw]) => {
    const q = record(raw);
    return q ? [`Column ${column}: ${q.present} valid values, ${q.missing} missing, ${q.invalid} invalid.`] : [];
  });
}

/** Existing activity disclosure, with the facts needed to judge coverage. */
export function AskToolActivity({ event }: { event: AskEvent }) {
  const p = event.payload;
  const range = record(p.range ?? p.scanned_range);
  const coverage = record(p.coverage);
  const denominators = Object.entries(record(p.denominators) ?? {});
  const groups = Array.isArray(p.groups) ? p.groups : [];
  const facts = qualityFacts(p.quality);
  for (const raw of groups) {
    const group = record(raw);
    if (!group) continue;
    const label = Array.isArray(group.group) ? group.group.map((part) => {
      const entry = record(part); return String(entry?.value ?? entry?.kind ?? '');
    }).join(', ') : '';
    facts.push(...qualityFacts(group.quality).map((fact) => label ? `${label} — ${fact}` : fact));
  }
  return <details className="ask-tool">
    <summary>{labels[String(p.tool)] ?? 'Reading sources'}{event.kind === 'tool_started' ? '…' : ''}</summary>
    {typeof p.detail === 'string' && <p>{p.detail}</p>}
    {p.query !== undefined && <p>Query: {typeof p.query === 'string' ? p.query : JSON.stringify(p.query)}</p>}
    {range && <p>Characters {String(range.start)}–{String(range.end)}{p.reached_end === false ? '; more remains.' : '.'}</p>}
    {typeof p.row_count === 'number' && <p>{p.row_count.toLocaleString()} rows considered.</p>}
    {typeof p.total === 'number' && <p>{p.total.toLocaleString()} matching rows.</p>}
    {typeof p.hits === 'number' && <p>{p.hits} search hits shown.</p>}
    {p.has_more === true && <p>More groups are available.</p>}
    {typeof p.excluded_null_groups === 'number' && p.excluded_null_groups > 0 && <p>{p.excluded_null_groups} groups had no value for the ranking metric.</p>}
    {facts.map((fact, index) => <p key={index}>{fact}</p>)}
    {denominators.map(([name, raw]) => {
      const d = record(raw);
      return d && <p key={name}>{name}: percentage denominator {String(d.value ?? 'unavailable')} across all filtered rows, before group filtering or pagination.{d.reason === 'zero_denominator' && ' Percentages are unavailable because the denominator is zero.'}{d.mixed_sign === true && ' Includes positive and negative values.'}</p>;
    })}
    {coverage?.semantic === false && coverage.complete === false && <p>Semantic coverage is incomplete; keyword results are shown.</p>}
    {typeof p.coverage === 'string' && <p>{p.coverage}</p>}
    {p.error !== undefined && <p>This step could not be completed. The assistant can try another query.</p>}
  </details>;
}
