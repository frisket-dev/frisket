import { render } from '@testing-library/react';
import { vi } from 'vitest';
import type { ComponentProps } from 'react';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { isGeneratedActionCatalogEntry } from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { servedActionCatalog } from './servedActionCatalog';

type Props = ComponentProps<typeof GeneratedActionForm>;
const catalog = servedActionCatalog();
export function renderYtdlpForm(props: Pick<Props, 'sheet'> & Partial<Props>) {
  const raw = catalog.actions.find((entry) => entry.kind === 'media.ytdlp_download');
  if (!raw || !isGeneratedActionCatalogEntry(raw)) throw new Error('Missing typed yt-dlp catalog');
  const onExecute = vi.fn();
  const resolveParams: Props['resolveParams'] = async ({ params }) => {
    const options = params.extra_opts as Record<string, unknown> | undefined;
    const active = new Set([params.media_type === 'audio' ? 'audio' : 'video']);
    if (options?.writethumbnail) active.add('thumbnail');
    if (options?.writeinfojson) active.add('info');
    if (options?.writesubtitles || options?.writeautomaticsub || options?.allsubtitles) {
      active.add('subtitles'); active.add('subtitles_text');
    }
    // Transport response mirrors the declared active-output contract; backend
    // tests cover its authoritative option validation and capability dispatch.
    return { diagnostics: {}, logical_outputs: raw.ui_hints.logical_outputs.filter(({ key }) => active.has(key)) };
  };
  const view = render(<GeneratedActionForm catalogEntry={raw}
    actionTemplate={generatedActionTemplateFromCatalogEntry(raw)!}
    running={false} onClose={() => {}} resolveParams={resolveParams}
    {...props} onExecute={onExecute} />);
  return { ...view, onExecute, entry: raw };
}
