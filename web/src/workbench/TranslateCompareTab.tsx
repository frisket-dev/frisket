import { CompareToolbar, CompareEngineChip, CompareResultColumn } from './CompareLayout';
import { Button } from '../components/Button';
import { FormField, TextArea, TextInput } from '../components/FormControls';
import { PanelBody } from '../components/PanelLayout';
import { PanelEmpty, PanelError } from '../components/PanelPrimitives';
import styles from './TranslateCompareTab.module.css';
import { useEffect, useMemo, useState } from 'react';
import { Loader2, Play } from 'lucide-react';

import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type { EngineOption, TranslateCompareEngineResult } from '../api/open';
import { translateEnginesFromCatalog } from '../actions/translateEngineCatalog';
import { engineIsRemote } from '../actions/engineCatalog';
import { CompareBillableEnginesNote } from './MediaCompareShell';
import { PanelSelect } from '../components/PanelSelect';
import { mediaSelectorQuery } from './mediaCompareSelector';
import { useCompareSelectorReadiness } from './useCompareSelectorReadiness';
import { useComparisonCatalog } from './useComparisonCatalog';
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
const NO_ENGINES: EngineOption[] = [];

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
  const { engines, status: catalogStatus, retry: retryCatalog } = useComparisonCatalog(translateEnginesFromCatalog, NO_ENGINES);
  const catalogLoading = catalogStatus === 'idle' || catalogStatus === 'loading';
  const catalogError = catalogStatus === 'error';
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [selectorEngineId, setSelectorEngineId] = useState<string | null>(null);
  const [text, setText] = useState('');
  const [targetLanguage, setTargetLanguage] = useState(DEFAULT_TARGET);
  const [sourceLanguage, setSourceLanguage] = useState('');
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState<TranslateCompareEngineResult[] | null>(null);
  const [runErrors, setRunErrors] = useState<Record<string, string>>({});
  const [runError, setRunError] = useState<string | null>(null);

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
  const selectorColumn = useMemo(() => ({ id: selectorEngineId ?? 'add', engineId: selectorEngineId,
    options: { language: sourceLanguage, target_language: targetLanguage } }), [selectorEngineId, sourceLanguage, targetLanguage]);
  const projectionColumns = useMemo(() => selectedColumns.some((column) => column.id === selectorColumn.id)
    ? selectedColumns : [...selectedColumns, selectorColumn], [selectedColumns, selectorColumn]);
  const { isRunnable, selectorProjection, selectorOwners } = useCompareSelectorReadiness(projectId, 'map.translate', projectionColumns);

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
      {selectorOwners}
      <CompareToolbar testIdPrefix="translate-compare" running={running} label="ENGINES" chipsTestId="translate-compare-engine-chips"
        controls={<>
          <Button
            type="button"
            className="btn-primary"
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
          </Button>
        </>}
        note={<CompareBillableEnginesNote
          testidPrefix="translate-compare"
          engines={billableEngines}
          actionLabel="the Translate action"
        />}
      >
        {selectedEngines.map((engine) => (
          <CompareEngineChip key={engine.id} testIdPrefix="translate-compare" engine={engine}
            engineId={engine.id} label={engine.label} onRemove={() => removeEngine(engine.id)} />
        ))}
        <div className="ocr-compare-add-wrap" data-testid="translate-compare-add-engine">
          {catalogError ? (
            <div role="alert" className="form-hint">
              Could not load comparison engines.{' '}
              <Button type="button" onClick={retryCatalog}>Retry</Button>
            </div>
          ) : catalogLoading ? (
            <span className="form-hint" role="status">Loading comparison engines…</span>
          ) : <SelectorField
            projectId={projectId}
            label="Add engine"
            query={mediaSelectorQuery('map.translate', selectorColumn)}
            projection={selectorProjection(selectorColumn)}
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
          />}
        </div>
      </CompareToolbar>

      <div className={`ocr-compare-body ${styles.workspace}`} data-testid="translate-compare-body">
        <PanelBody padding="compact" className={styles.source} data-testid="translate-compare-source">
          <div className="content-stack">
            <FormField label="Sample text">
              <TextArea
                data-testid="translate-compare-text"
                rows={8}
                value={text}
                placeholder="Paste a sentence or two to translate with every selected engine…"
                onChange={(event) => setText(event.target.value)}
              />
            </FormField>
            <FormField label="Translate from">
              <PanelSelect
                className="row-height-select"
                testId="translate-compare-source-language"
                ariaLabel="Translate from"
                value={sourceLanguage}
                onValueChange={setSourceLanguage}
                options={SOURCE_LANGUAGE_OPTIONS}
              />
            </FormField>
            <FormField label="Target language">
              <TextInput
                data-testid="translate-compare-target-language"
                value={targetLanguage}
                placeholder="e.g. Japanese, Spanish, French"
                onChange={(event) => setTargetLanguage(event.target.value)}
              />
            </FormField>
            {runError && <PanelError testId="translate-compare-run-error">{runError}</PanelError>}
          </div>
        </PanelBody>

        {results !== null ? (
          <div className={`ocr-compare-columns ${styles.results}`} data-testid="translate-compare-results">
            {selectedEngines.map((engine) => {
              const result = resultByEngine.get(engine.id);
              const error = runErrors[engine.id] ?? (result?.errors ?? [])[0];
              return (
                <CompareResultColumn key={engine.id}
                  data-testid="translate-compare-result-card"
                  data-engine-id={engine.id}
                  header={<>
                    <span className="ocr-compare-variant-name">{engine.label}</span>
                    {result?.detected_language ? (
                      <span
                        className="ocr-compare-variant-summary muted"
                        data-testid={`translate-compare-detected-${engine.id}`}
                      >
                        detected: {result.detected_language}
                      </span>
                    ) : null}
                  </>}

                >
                  {error ? (
                    <PanelError testId={`translate-compare-result-error-${engine.id}`}>
                      {error}
                    </PanelError>
                  ) : (
                    <p
                      className="ocr-compare-plain-text"
                      data-testid={`translate-compare-result-text-${engine.id}`}
                    >
                      {result?.translation || '—'}
                    </p>
                  )}
                </CompareResultColumn>
              );
            })}
          </div>
        ) : (
          <PanelBody padding="compact">
            <PanelEmpty>Enter sample text and choose engines to compare translations.</PanelEmpty>
          </PanelBody>
        )}
      </div>
    </section>
  );
}
