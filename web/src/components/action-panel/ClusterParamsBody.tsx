import { useEffect, useMemo, useRef, useState } from 'react';
import { Search } from 'lucide-react';
import { ApiError, type ClusterGroup, type ClusterValuesMethod } from '../../api/open';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { PanelSelect } from '../PanelSelect';
import type { GeneratedActionParams } from '../../generated/actionTypes';
import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';
import '../cluster-review.css';

type Params = GeneratedActionParams['cluster.values'];
type Review = NonNullable<Params['review']>;
const METHODS: { value: ClusterValuesMethod; label: string; hint: string }[] = [
  { value: 'fingerprint', label: 'Fingerprint (token collision)',
    hint: 'Groups values sharing the same normalized word set — “Jon Smith” / “Smith, Jon”.' },
  { value: 'ngram_fingerprint', label: 'N-gram fingerprint (typos & spacing)',
    hint: 'Character n-grams catch typos and spacing variants fingerprint misses — “Sao Paulo” / “SaoPaulo”.' },
  { value: 'semantic', label: 'Semantic (meaning, needs embeddings)',
    hint: 'Groups by meaning — “WHO” / “World Health Organization”. Requires an embedding backend.' },
];

function savedReviewIssue(review: Review, clusters: ClusterGroup[]): string | null {
  const byKey = new Map(clusters.map((cluster) => [cluster.key, cluster]));
  for (const [key, canonical] of Object.entries(review.canonical_overrides ?? {})) {
    if (!byKey.has(key) || !canonical.trim()) {
      return `Saved canonical edit for group "${key}" no longer matches the current preview.`;
    }
  }
  for (const [key, surfaces] of Object.entries(review.excluded_members ?? {})) {
    const current = new Set(byKey.get(key)?.values.map((value) => value.value) ?? []);
    if (!byKey.has(key) || surfaces.some((surface) => !current.has(surface))) {
      return `Saved member exclusions for group "${key}" no longer match the current preview.`;
    }
  }
  return null;
}

/** Only the reviewed Params editor. The generated host owns naming and writing. */
export function ClusterParamsBody(props: GeneratedActionParamsBodyProps<'cluster.values'>) {
  if (!props.sheet) return <p className="form-hint">Choose a source sheet to configure this action.</p>;
  return <ClusterParamsBodyForSheet {...props} sheet={props.sheet} />;
}

function ClusterParamsBodyForSheet({ sheet, params, setParams, setEditorProblem }:
  GeneratedActionParamsBodyProps<'cluster.values'> & {
    sheet: NonNullable<GeneratedActionParamsBodyProps<'cluster.values'>['sheet']>;
  }) {
  const { projectApi: api } = useWorkspaceStores();
  const columns = useMemo(() => sheet.columns.filter((column) =>
    ['text', 'category', 'link'].includes(column.type)), [sheet.columns]);
  const source = params.source;
  const method = params.method ?? 'fingerprint';
  const minSize = params.min_size ?? 2;
  const threshold = params.threshold ?? 0.85;
  const ngramSize = params.ngram_size ?? 2;
  const keyTemplate = params.key_template ?? '';
  const sourceExists = columns.some((column) => column.name === source);
  const sourceId = columns.find((column) => column.name === source)?.id;
  const identity = JSON.stringify([sheet.id, sourceId, source, method, minSize,
    params.threshold, params.ngram_size, params.key_template]);
  const currentIdentity = useRef(identity);
  useEffect(() => { currentIdentity.current = identity; }, [identity]);
  const [reviewed, setReviewed] = useState<{
    identity: string; hash: string; clusters: ClusterGroup[];
  } | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [excluded, setExcluded] = useState<Record<string, string[]>>({});
  const [savedReview, setSavedReview] = useState<Review | null>(() => params.review ?? null);
  const [savedRepair, setSavedRepair] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const generation = useRef(0);
  useEffect(() => () => { generation.current += 1; }, []);
  const currentReview = reviewed?.identity === identity ? reviewed : null;
  const hasGroups = Boolean(currentReview?.clusters.length);
  const blankCanonical = currentReview?.clusters.some((cluster) =>
    !(drafts[cluster.key] ?? cluster.canonical).trim());
  const problem = !sourceExists ? 'Choose an available column to cluster.'
    : busy ? 'Wait for the cluster preview to finish.'
    : savedRepair ? 'Resolve the incompatible saved review before writing.'
    : !hasGroups ? 'Preview the groups before writing.'
    : blankCanonical ? 'Every group needs a canonical value.' : null;
  useEffect(() => { setEditorProblem?.(problem); }, [problem, setEditorProblem]);

  const update = (changes: Partial<Params>) => {
    generation.current += 1;
    setReviewed(null);
    setDrafts({});
    setExcluded({});
    setSavedReview(null);
    setSavedRepair(null);
    setError(null);
    setUnavailable(false);
    setBusy(false);
    const next = { ...params, ...changes };
    delete next.review;
    if (next.threshold === undefined) delete next.threshold;
    if (next.ngram_size === undefined) delete next.ngram_size;
    setParams(next);
  };

  // Only a currently displayed, successfully reviewed snapshot enters Params.
  useEffect(() => {
    if (!currentReview || savedRepair || blankCanonical) return;
    const overrides: Record<string, string> = {};
    const drops: Record<string, string[]> = {};
    for (const cluster of currentReview.clusters) {
      const canonical = (drafts[cluster.key] ?? cluster.canonical).trim();
      if (canonical !== cluster.canonical) overrides[cluster.key] = canonical;
      const members = cluster.values.map((value) => value.value)
        .filter((surface) => excluded[cluster.key]?.includes(surface));
      if (members.length) drops[cluster.key] = members;
    }
    const review: Review = { source_hash: currentReview.hash,
      canonical_overrides: overrides, excluded_members: drops };
    if (JSON.stringify(params.review) !== JSON.stringify(review)) setParams({ ...params, review });
  }, [currentReview, savedRepair, blankCanonical, drafts, excluded, params, setParams]);

  const preview = async () => {
    if (!sourceExists) return;
    const seq = ++generation.current;
    const requestedIdentity = identity;
    setBusy(true);
    setError(null);
    setUnavailable(false);
    try {
      const result = await api.clusterPreview({ sheetId: sheet.id, inputColumn: source,
        method, minSize, threshold: method === 'semantic' ? threshold : undefined,
        ngramSize: method === 'ngram_fingerprint' ? ngramSize : undefined,
        keyTemplate: keyTemplate.trim() || undefined });
      if (seq !== generation.current || requestedIdentity !== currentIdentity.current) return;
      const nextDrafts = Object.fromEntries(result.clusters.map((cluster) => [cluster.key, cluster.canonical]));
      let nextExcluded: Record<string, string[]> = {};
      const issue = savedReview ? savedReviewIssue(savedReview, result.clusters) : null;
      if (issue) setSavedRepair(`${issue} Discard the incompatible saved edits to use these current groups.`);
      else {
        Object.assign(nextDrafts, savedReview?.canonical_overrides ?? {});
        nextExcluded = { ...savedReview?.excluded_members };
        setSavedReview(null);
        setSavedRepair(null);
      }
      setDrafts(nextDrafts);
      setExcluded(nextExcluded);
      setReviewed({ identity: requestedIdentity, hash: result.valueHash, clusters: result.clusters });
      if (!result.count) setError('No duplicate groups found.');
    } catch (cause) {
      if (seq !== generation.current || requestedIdentity !== currentIdentity.current) return;
      setUnavailable(cause instanceof ApiError && cause.code === 'embedding_backend_unavailable');
      setError(cause instanceof Error ? cause.message : String(cause));
      setReviewed(null);
    } finally {
      if (seq === generation.current) setBusy(false);
    }
  };

  return <div className="cluster-review-form" data-testid="cluster-review-form">
    <div className="cluster-controls">
      <label className="field-group"><span className="field-labels">Column</span>
        <PanelSelect className="form-input" data-testid="cluster-column-select"
          value={source} disabled={busy} onChange={(event) => update({ source: event.target.value })}>
          {!sourceExists && <option value={source}>{source ? `${source} (unavailable)` : 'Choose a column'}</option>}
          {columns.map((column) => <option key={column.id} value={column.name}>{column.name}</option>)}
        </PanelSelect>
      </label>
      <label className="field-group"><span className="field-labels">Method</span>
        <PanelSelect className="form-input" data-testid="cluster-method-select" value={method}
          disabled={busy} onChange={(event) => update({ method: event.target.value as ClusterValuesMethod,
            threshold: undefined, ngram_size: undefined })}>
          {METHODS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </PanelSelect>
      </label>
      <p className="form-hint" data-testid="cluster-method-hint">{METHODS.find((item) => item.value === method)?.hint}</p>
      <details className="cluster-advanced" data-testid="cluster-advanced">
        <summary>Advanced: cluster key</summary>
        <label className="field-group"><span className="field-labels">Cluster key template</span>
          <input className="form-input" data-testid="cluster-key-template" placeholder="{{value}}"
            value={keyTemplate} disabled={busy} onChange={(event) => update({ key_template: event.target.value })} />
          <p className="form-hint">Cluster on a transformed value while merging the original forms.
            Example: <code>{'{{value|before:" of "}}'}</code> groups “President of Honduras” with “President”.</p>
        </label>
      </details>
      <label className="field-group cluster-min-size"><span className="field-labels">Min forms</span>
        <input className="form-input" type="number" data-testid="cluster-min-size" min={2} max={20}
          value={minSize} disabled={busy} onChange={(event) => update({ min_size: Math.max(2, Number(event.target.value) || 2) })} />
      </label>
      {method === 'semantic' && <label className="field-group" data-testid="cluster-threshold-field">
        <span className="field-labels">Similarity threshold</span>
        <input className="form-input" type="number" min={0} max={1} step={0.01} value={threshold}
          disabled={busy} onChange={(event) => update({ threshold: Number(event.target.value) })} />
      </label>}
      {method === 'ngram_fingerprint' && <label className="field-group" data-testid="cluster-ngram-size-field">
        <span className="field-labels">N-gram size</span>
        <input className="form-input" type="number" min={1} max={6} value={ngramSize}
          disabled={busy} onChange={(event) => update({ ngram_size: Number(event.target.value) })} />
      </label>}
    </div>
    {unavailable && <div className="form-error" data-testid="cluster-semantic-unavailable">
      {error} Choose Fingerprint or N-gram fingerprint to cluster without embeddings.
    </div>}
    {error && !unavailable && <div className="form-error" data-testid="cluster-error">{error}</div>}
    {savedRepair && <div className="form-error" data-testid="cluster-saved-review-repair" role="alert">
      {savedRepair}{' '}<button type="button" className="btn" data-testid="cluster-saved-review-reset"
        onClick={() => { setSavedReview(null); setSavedRepair(null); }}>Use current groups</button>
    </div>}
    {hasGroups && currentReview && <div className="cluster-list">
      {currentReview.clusters.map((cluster) => {
        const value = drafts[cluster.key] ?? cluster.canonical;
        return <div className="cluster-card" data-testid="cluster-card" key={cluster.key}>
          <div className="cluster-card-top"><span>{cluster.size.toLocaleString()} mentions</span>
            <span>{cluster.values.length.toLocaleString()} forms</span></div>
          <label className="field-group"><span className="field-labels">Canonical</span>
            <input className={value.trim() ? 'form-input' : 'form-input form-input-invalid'}
              data-testid="cluster-canonical-input" aria-invalid={!value.trim()} value={value}
              onChange={(event) => setDrafts((before) => ({ ...before, [cluster.key]: event.target.value }))} />
            {!value.trim() && <span className="form-error" data-testid="cluster-canonical-blank">A canonical value is required.</span>}
          </label>
          <ul className="cluster-values">{cluster.values.map((member) => <li key={member.value}>
            <label className="cluster-member"><input type="checkbox" data-testid="cluster-member-checkbox"
              checked={!excluded[cluster.key]?.includes(member.value)} onChange={() => setExcluded((before) => {
                const members = before[cluster.key] ?? [];
                return { ...before, [cluster.key]: members.includes(member.value)
                  ? members.filter((surface) => surface !== member.value) : [...members, member.value] };
              })} /><span>{member.value}</span></label><span>{member.count.toLocaleString()}</span>
          </li>)}</ul>
        </div>;
      })}
    </div>}
    <button type="button" className="btn" data-testid="cluster-preview-button"
      disabled={busy || !sourceExists} onClick={() => { void preview(); }}>
      <Search size={13} /> {busy ? 'Working…' : 'Preview groups'}
    </button>
  </div>;
}
