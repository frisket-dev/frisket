import { useMemo, useRef, useState } from 'react';
import { Check, ChevronDown } from 'lucide-react';
import { useNativePopover } from '../../hooks/useNativePopover';
import { useAnchoredPosition } from '../../hooks/useAnchoredPosition';
import type { EmbeddingProvider } from '../../api/types';

function formatSize(sizeGb: number | null): string | null {
  if (sizeGb == null) return null;
  return `${sizeGb.toFixed(2)} GB`;
}

interface EmbeddingModelPickerProps {
  models: EmbeddingProvider[];
  provider: string;
  model: string;
  onSelect(provider: string, model: string): void;
}

/** A self-contained card picker over a list of embedding models (the
 *  provider-catalog projection). */
export function EmbeddingModelPicker({
  models,
  provider,
  model,
  onSelect,
}: EmbeddingModelPickerProps) {
  const [open, setOpen] = useState(false);
  const [custom, setCustom] = useState('');
  // The provider a custom-id submission is bound to. A discovery-required remote
  // card (OpenRouter) pins this so the typed id is sent with the remote provider
  // (not the local fastembed fallback).
  const [customProvider, setCustomProvider] = useState('');
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  // recommended first, then available, then the rest - stable within groups.
  const sorted = useMemo(() => {
    const rank = (m: EmbeddingProvider) =>
      m.recommended ? 0 : m.available && m.modalityCompatible ? 1 : 2;
    const ranked: Array<{ model: EmbeddingProvider; index: number }> = [];
    for (let index = 0; index < models.length; index += 1) {
      ranked.push({ model: models[index], index });
    }
    ranked.sort((a, b) => rank(a.model) - rank(b.model) || a.index - b.index);
    const ordered: EmbeddingProvider[] = [];
    for (const item of ranked) ordered.push(item.model);
    return ordered;
  }, [models]);

  const selectedCard =
    models.find((m) => m.providerId === provider && m.modelId === model) ?? null;
  // A typed id that matches no card = a custom selection.
  const isCustom = provider !== '' && model !== '' && selectedCard == null;

  const fieldLabel = isCustom
    ? `Custom · ${model}`
    : selectedCard
      ? selectedCard.label
      : 'Choose a model…';

  // Top-layer popover: plain ref+toggle default dismissal (Escape / outside click).
  useNativePopover(menuRef, () => setOpen(false), {
    enabled: open,
    ignoreSelector: '[data-testid="embedding-model-field"]',
  });
  // Fixed-position anchor: as a top-layer element, the popover doesn't inherit
  // placement from `.embedding-model-picker`'s CSS-positioned ancestor
  // (`.embedding-model-popover` otherwise spans the full wrapper width).
  const menuPos = useAnchoredPosition(triggerRef, {
    enabled: open,
    align: 'left',
    width: (rect) => rect.width,
    gap: 4,
  });

  const choose = (m: EmbeddingProvider) => {
    // A discovery-required remote card (OpenRouter) has no single model id - it is
    // a bring-your-own-id affordance.
    if (m.dimensionDiscoveryRequired) {
      setCustomProvider(m.providerId);
      setCustom('');
      onSelect(m.providerId, '');
      return;
    }
    onSelect(m.providerId, m.modelId);
    setCustom('');
    setCustomProvider('');
    setOpen(false);
  };

  const applyCustom = (value: string) => {
    setCustom(value);
    const id = value.trim();
    if (id === '') return;
    const fallbackProvider =
      customProvider ||
      selectedCard?.providerId ||
      provider ||
      models[0]?.providerId ||
      '';
    onSelect(fallbackProvider, id);
  };

  return (
    <div className="embedding-model-picker" ref={rootRef}>
      <button
        type="button"
        ref={triggerRef}
        className="form-input embedding-model-field"
        data-testid="embedding-model-field"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="embedding-model-field-label">{fieldLabel}</span>
        <ChevronDown size={13} aria-hidden />
      </button>

      {open && (
        <div
          ref={menuRef}
          className="embedding-model-popover"
          data-testid="embedding-model-popover"
          style={
            menuPos
              ? { position: 'fixed', inset: 'auto', top: menuPos.top, bottom: menuPos.bottom, left: menuPos.left, right: 'auto', width: menuPos.width, margin: 0 }
              : { position: 'fixed', visibility: 'hidden' }
          }
        >
          <div className="embedding-model-cards">
            {sorted.map((m) => {
              const disabled = !m.available || !m.modalityCompatible;
              const dim = m.dimensions?.[0] ?? null;
              const size = formatSize(m.sizeGb);
              const selected = m.providerId === provider && m.modelId === model;
              return (
                <button
                  type="button"
                  key={`${m.providerId}:${m.modelId}`}
                  className={`embedding-model-card${selected ? ' is-selected' : ''}${
                    disabled ? ' is-disabled' : ''
                  }`}
                  data-testid={
                    disabled
                      ? `embedding-model-card-disabled-${m.modelId}`
                      : `embedding-model-card-${m.modelId}`
                  }
                  disabled={disabled}
                  aria-disabled={disabled}
                  aria-pressed={selected}
                  onClick={() => !disabled && choose(m)}
                >
                  <span className="embedding-model-card-head">
                    <span className="embedding-model-card-label">{m.label}</span>
                    {m.recommended && (
                      <span
                        className="embedding-model-badge"
                        data-testid="embedding-model-recommended-badge"
                      >
                        Recommended
                      </span>
                    )}
                    {selected && !disabled && (
                      <Check size={13} aria-hidden className="embedding-model-card-check" />
                    )}
                  </span>
                  <span className="embedding-model-card-meta">
                    {dim != null && <span>{dim}-dim</span>}
                    {dim == null && m.dimensionDiscoveryRequired && (
                      <span>dimension found at create</span>
                    )}
                    {size !== null && <span>{size}</span>}
                    <span>
                      {m.local ? 'on your machine' : `remote · ${m.providerId}`}
                    </span>
                  </span>
                  {disabled && (
                    <span className="embedding-model-card-reason">
                      {m.disabledReason ??
                        (!m.modalityCompatible ? 'incompatible with this column' : 'unavailable')}
                    </span>
                  )}
                </button>
              );
            })}
          </div>

          <div className="embedding-model-custom">
            <label className="form-label" htmlFor="embedding-model-custom-input">
              {customProvider
                ? `Custom remote model id (${customProvider})`
                : 'Custom model…'}
            </label>
            <input
              id="embedding-model-custom-input"
              className="form-input"
              data-testid="embedding-model-custom-input"
              placeholder={
                customProvider
                  ? 'e.g. openai/text-embedding-3-large'
                  : 'e.g. BAAI/bge-small-en-v1.5'
              }
              value={isCustom ? custom || model : custom}
              onChange={(e) => applyCustom(e.target.value)}
            />
            <p className="form-hint">
              A local fastembed id, or a remote provider id (e.g. an OpenRouter
              model). The backend validates it at create - a remote dimension is
              discovered with one probe embed after you confirm egress.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
