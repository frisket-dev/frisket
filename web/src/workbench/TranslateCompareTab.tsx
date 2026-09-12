import { useEffect, useMemo, useState } from 'react';
import { Loader2, Play, X } from 'lucide-react';

import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type { EngineOption, TranslateCompareEngineResult } from '../api/open';
import { translateEnginesFromCatalog } from '../actions/translateEngineCatalog';
import { engineIsRemote, tierForEngine } from '../actions/engineCatalog';
import { EngineTierBadge } from '../components/EngineTierBadge';
import { CompareBillableEnginesNote } from './MediaCompareShell';
import { PanelSelect } from '../components/PanelSelect';
import { mediaSelectorQuery } from './mediaCompareSelector';
import { useCompareSelectorReadiness } from './useCompareSelectorReadiness';
import { SelectorField } from '../engine-selector/SelectorField';

// TranslateCompareTab — the TEXT-source sibling of the OCR/Transcribe
// compare tabs. Those are built on MediaCompareShell (file drop + media peek
// + segment alignment); translation input is pasted text, so this is a
// lightweight, purpose-built surface that reuses only the small pieces
// (engine chips + add-menu + the shared ocr-compare-* styling), NOT the media
// shell. It consumes the same translateEnginesFromCatalog source the translate
// ACTION form uses (no drift: single-pick there, multi-chip here), and the
// POST /translate/compare-scratch endpoint.
//
// FREE ENGINES ONLY. Billable -> run; not billable -> preview. deepl, google
// and llm all bill per call, and a bake-off that spends money at a provider
// endpoint already IS a run — so they are filtered out of the picker here and
// refused by the endpoint, with CompareBillableEnginesNote pointing at the
// cost-gated map.translate action. That is why the old allow_remote consent
// checkbox is gone: there is nothing left to consent to on this surface.
//
// Staged Run (not auto-run): unlike the media tabs a text sample has no drop
// event to fire on, so the user edits text + engines freely, then Runs once.

const DEFAULT_TARGET = 'Spanish';

// Auto + a few common source hints (single-select; '' = auto-detect). Kept
// short — the target box is free text; the source is only a hint for engines
// that accept one.
const SOURCE_LANGUAGE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '', label: 'Auto-detect' },
  { value: 'en', label: 'English' },
  { value: 'es', label: 'Spanish' },
  { value: 'fr', label: 'French' },
  { value: 'de', label: 'German' },
  { value: 'pt', label: 'Portuguese' },
  { value: 'zh', label: 'Chinese' },
  { value: 'ja', label: 'Japanese' },
];

interface TranslateCompareTabProps {
  onSessionChange(state: { hasData: boolean; verdict: string }): void;
}

export function TranslateCompareTab({ onSessionChange }: TranslateCompareTabProps) {
  const { projectApi: api, chromePreferences: { projectId } } = useWorkspaceStores();
  const [engines, setEngines] = useState<EngineOption[]>([]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [selectorEngineId, setSelectorEngineId] = useState<string | null>(null);
  const [text, setText] = useState('');
  const [targetLanguage, setTargetLanguage] = useState(DEFAULT_TARGET);
  const [sourceLanguage, setSourceLanguage] = useState('');
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState<TranslateCompareEngineResult[] | null>(null);
  const [runErrors, setRunErrors] = useState<Record<string, string>>({});
  const [runError, setRunError] = useState<string | null>(null);

  // This catalog only supplies compare labels and its existing free/billed
  // policy. The selector service owns offered choices and readiness.
  useEffect(() => {
    let cancelled = false;
    api
      .listActionCatalog()
      .then((catalog) => {
        // THE filter point: billable engines never enter the picker, so a
        // compare column cannot hold one and there is no consent gate to
        // forget. `billableEngines` keeps them for the explanatory note.
        if (!cancelled) setEngines(translateEnginesFromCatalog(catalog));
      })
      .catch(() => { if (!cancelled) setEngines([]); });
    return () => {
      cancelled = true;
    };
  }, []);

  const freeEngines = useMemo(
    () => engines.filter((engine) => !engineIsRemote(engine)),
    [engines],
  );
  const billableEngines = useMemo(
    () => engines.filter((engine) => engineIsRemote(engine)),
    [engines],
  );
  const engineById = useMemo(
    () => new Map(freeEngines.map((engine) => [engine.id, engine])),
    [freeEngines],
  );
  const selectedEngines = selectedIds.map(
    (id) => engineById.get(id) ?? ({ id, label: id, tier: 'local' } as EngineOption),
  );

  const hasData = Boolean(text.trim()) || results !== null;
  useEffect(() => {
    onSessionChange({
      hasData,
      verdict: `${selectedIds.length} engine${selectedIds.length === 1 ? '' : 's'}`,
    });
  }, [hasData, selectedIds.length, onSessionChange]);

  const selectedColumns = useMemo(() => selectedIds.map((id) => ({
    id, engineId: id, options: { language: sourceLanguage, target_language: targetLanguage },
  })), [selectedIds, sourceLanguage, targetLanguage]);
  const { isRunnable, reportColumnChoice } = useCompareSelectorReadiness(projectId, 'map.translate', selectedColumns);
  const selectorColumn = { id: selectorEngineId ?? 'add', engineId: selectorEngineId,
    options: { language: sourceLanguage, target_language: targetLanguage } };

  const canRun =
    Boolean(text.trim()) && selectedIds.length > 0 && !running
    && selectedColumns.every((column) => engineById.has(column.id) && isRunnable(column));

  const removeEngine = (id: string) => {
    setSelectedIds((ids) => ids.filter((candidate) => candidate !== id));
    setSelectorEngineId((current) => current === id ? null : current);
  };

  const addEngine = (id: string) => {
    setSelectedIds((ids) => (ids.includes(id) ? ids : [...ids, id]));
    setSelectorEngineId(id);
  };

  async function run() {
    if (!canRun) return;
    setRunning(true);
    setRunErrors({});
    setRunError(null);
    try {
      const result = await api.compareTranslateScratch({
        engines: selectedIds,
        text,
        targetLanguage,
        language: sourceLanguage || null,
      });
      setResults(result.results);
      const errs: Record<string, string> = {};
      for (const item of result.errors ?? []) {
        if (item.engine) errs[item.engine] = item.message ?? item.code ?? 'engine failed';
      }
      setRunErrors(errs);
      onSessionChange({ hasData: true, verdict: `${result.results.length} engines` });
    } catch (err) {
      setRunError(err instanceof Error ? err.message : 'Translate compare failed');
    } finally {
      setRunning(false);
    }
  }

  const resultByEngine = useMemo(() => {
    const map = new Map<string, TranslateCompareEngineResult>();
    for (const item of results ?? []) map.set(item.engine, item);
    return map;
  }, [results]);

  return (
    <section className="ocr-compare-tab" data-testid="translate-compare-tab">
      <div
        className="ocr-compare-toolbar"
        data-testid="translate-compare-toolbar"
        data-running={running ? 'true' : 'false'}
      >
        <div className="ocr-compare-chips-row" data-testid="translate-compare-chips-row">
          <span className="ocr-compare-scratch-tag" data-testid="translate-compare-scratch-tag">
            scratch · not in a project
          </span>
          <span className="ocr-compare-variants-label">ENGINES</span>
          <div
            className="ocr-compare-variant-chips"
            data-testid="translate-compare-engine-chips"
          >
            {selectedEngines.map((engine) => (
              <div
                key={engine.id}
                className="ocr-compare-variant-chip engine-tier-chip"
                data-testid="translate-compare-engine-chip"
                data-engine-id={engine.id}
              >
                <span className="ocr-compare-variant-name">{engine.label}</span>
                {/* Where the chosen engine runs, visible on the chip
                    itself. */}
                <EngineTierBadge
                  tier={tierForEngine(engine)}
                  testId="translate-compare-engine-chip-tier"
                />
                <button
                  type="button"
                  className="ocr-compare-variant-remove"
                  data-testid={`translate-compare-engine-chip-remove-${engine.id}`}
                  aria-label={`Remove ${engine.label}`}
                  onClick={() => removeEngine(engine.id)}
                >
                  <X size={11} />
                </button>
              </div>
            ))}
            <div className="ocr-compare-add-wrap" data-testid="translate-compare-add-engine">
              <SelectorField
                projectId={projectId}
                label="Add engine"
                query={mediaSelectorQuery('map.translate', selectorColumn)}
                onCurrentChoiceChange={(choice) => reportColumnChoice(selectorColumn, choice)}
                recentNamespace={`${projectId}:translate-compare`}
                allowChoice={(choice) => {
                  const authored = choice.authored_selection;
                  const engineId = authored.kind === 'engine' || authored.kind === 'engine_model'
                    ? authored.engine : null;
                  return engineId !== null
                    && !selectedIds.includes(engineId)
                    && freeEngines.some((engine) => engine.id === engineId);
                }}
                onSelect={(choice) => {
                  const authored = choice.authored_selection;
                  if (authored.kind === 'engine' || authored.kind === 'engine_model') addEngine(authored.engine);
                }}
              />
            </div>
          </div>
        </div>
      </div>

      <div className="ocr-compare-body" data-testid="translate-compare-body">
        <label className="ocr-compare-configure-field">
          <span className="ocr-compare-configure-label">Sample text</span>
          <textarea
            className="form-input"
            data-testid="translate-compare-text"
            rows={5}
            value={text}
            placeholder="Paste a sentence or two to translate with every selected engine…"
            onChange={(event) => setText(event.target.value)}
          />
        </label>

        <div className="ocr-compare-controls-row">
          <label className="ocr-compare-configure-field">
            <span className="ocr-compare-configure-label">Translate from</span>
            <PanelSelect
              className="row-height-select"
              testId="translate-compare-source-language"
              ariaLabel="Translate from"
              value={sourceLanguage}
              onValueChange={setSourceLanguage}
              options={SOURCE_LANGUAGE_OPTIONS}
            />
          </label>
          <label className="ocr-compare-configure-field">
            <span className="ocr-compare-configure-label">Target language</span>
            <input
              className="form-input"
              data-testid="translate-compare-target-language"
              value={targetLanguage}
              placeholder="e.g. Japanese, Spanish, French"
              onChange={(event) => setTargetLanguage(event.target.value)}
            />
          </label>
        </div>

        <CompareBillableEnginesNote
          testidPrefix="translate-compare"
          engines={billableEngines}
          actionLabel="the Translate action"
        />

        <div className="ocr-compare-controls-row">
          <button
            type="button"
            className="btn btn-primary"
            data-testid="translate-compare-run"
            disabled={!canRun}
            title={
              !text.trim()
                ? 'Paste some text first'
                : selectedIds.length === 0
                  ? 'Add at least one engine'
                  : undefined
            }
            onClick={run}
          >
            {running ? <Loader2 size={13} className="ocr-compare-spin" /> : <Play size={13} />}{' '}
            {running ? 'Translating…' : 'Translate'}
          </button>
        </div>

        {runError ? (
          <p className="form-hint" data-testid="translate-compare-run-error">
            {runError}
          </p>
        ) : null}

        {results !== null ? (
          <div className="ocr-compare-results" data-testid="translate-compare-results">
            {selectedEngines.map((engine) => {
              const result = resultByEngine.get(engine.id);
              const error = runErrors[engine.id] ?? (result?.errors ?? [])[0];
              return (
                <div
                  key={engine.id}
                  className="ocr-compare-result-card"
                  data-testid="translate-compare-result-card"
                  data-engine-id={engine.id}
                >
                  <div className="ocr-compare-result-head">
                    <span className="ocr-compare-variant-name">{engine.label}</span>
                    {result?.detected_language ? (
                      <span
                        className="ocr-compare-variant-summary muted"
                        data-testid={`translate-compare-detected-${engine.id}`}
                      >
                        detected: {result.detected_language}
                      </span>
                    ) : null}
                  </div>
                  {error ? (
                    <p
                      className="form-hint ocr-compare-result-error"
                      data-testid={`translate-compare-result-error-${engine.id}`}
                    >
                      {error}
                    </p>
                  ) : (
                    <p
                      className="ocr-compare-result-text"
                      data-testid={`translate-compare-result-text-${engine.id}`}
                    >
                      {result?.translation || '—'}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        ) : null}
      </div>
    </section>
  );
}
