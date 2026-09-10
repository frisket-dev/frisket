import { render } from '@testing-library/react';
import { vi } from 'vitest';
import type { ComponentProps } from 'react';
import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { isGeneratedActionCatalogEntry } from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { servedActionCatalog } from './servedActionCatalog';

type Props = ComponentProps<typeof GeneratedActionForm>;
const catalog = servedActionCatalog();
const templates = actionTemplatesFromCatalog(catalog);

export function renderMediaAction(kind: string, props: Pick<Props, 'sheet'> & Partial<Props>) {
  const entry = catalog.actions.find((entry) => entry.kind === kind);
  if (!entry || !isGeneratedActionCatalogEntry(entry)) throw new Error(`Missing typed media action ${kind}`);
  const template = templates.find((item) => item.kind === kind)!;
  const onExecute = vi.fn(props.onExecute);
  const onNavigateToAction = vi.fn(props.onNavigateToAction);
  const resolveParams = vi.fn(async ({ params }: Parameters<Props['resolveParams']>[0]) => {
    const diagnostics: Record<string, { ok: boolean; message: string }> = {};
    if (kind === 'web.capture_page') {
      const links = params.output_mode === 'links';
      return { diagnostics, creates_sheet: links, logical_outputs: links
        ? ['url', 'anchor_text', 'source_url'].map((key) => ({ key,
          column_type: key === 'anchor_text' ? 'text' : 'link' }))
        : [{ key: 'page', column_type: 'file', existing_column_policy: 'compatible' as const }] };
    }
    if (kind === 'media.video_frames') {
      const sampling = params.sampling as { kind: string; count?: number; seconds?: number } | undefined;
      if (sampling && (sampling.kind === 'count'
        ? !Number.isInteger(sampling.count) || sampling.count! < 1 || sampling.count! > 200
        : !Number.isFinite(sampling.seconds) || sampling.seconds! < 0.1)) {
        diagnostics.sampling = { ok: false, message: 'Choose a valid frame count or interval.' };
      }
      if (params.max_dimension != null && (!Number.isInteger(params.max_dimension)
        || Number(params.max_dimension) < 16 || Number(params.max_dimension) > 4096)) {
        diagnostics.max_dimension = { ok: false, message: 'Enter a value between 16 and 4096 pixels.' };
      }
    }
    return { diagnostics, logical_outputs: entry.ui_hints.logical_outputs };
  });
  return { ...render(<GeneratedActionForm catalogEntry={entry} actionTemplate={template}
    running={false} onClose={() => {}} resolveParams={resolveParams} {...props}
    onExecute={onExecute} onNavigateToAction={onNavigateToAction} />),
  onExecute, onNavigateToAction, resolveParams, catalog, entry };
}
