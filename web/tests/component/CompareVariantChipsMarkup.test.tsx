// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/media/pdfjsSetup', () => ({ pdfjsLib: { getDocument: vi.fn() } }));

import { selectorChoicesApi } from '../../src/api/selectorChoices';
import { createProjectApi } from '../../src/api/real';
import { OcrCompareTab } from '../../src/workbench/OcrCompareTab';
import { TopicSegmentationCompareTab } from '../../src/workbench/TopicSegmentationCompareTab';
import { TranscribeCompareTab } from '../../src/workbench/TranscribeCompareTab';
import { installDialogPolyfill } from '../support/domPolyfills';
import { mediaChoices } from '../support/mediaSelectorFixtures';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

// Pins the rendered variant-chip toolbar of every media compare tab in each
// state the chip row can take: idle, Configure open, blank variant, busy and
// run error.

beforeAll(installDialogPolyfill);

const api = createProjectApi('test-project');
const harness = createWorkspaceTestHarness({ projectId: 'test-project', api: { projectApi: api } });
const render: typeof harness.render = (...args) => {
  void harness.stores.actionCatalog.start();
  return harness.render(...args);
};

const CATALOG = {
  'media.ocr': [
    { id: 'rapidocr', label: 'RapidOCR', tier: 'local', available: true },
    { id: 'dots.mocr', label: 'dots.mocr', tier: 'sidecar', available: true },
  ],
  'media.transcribe': [
    {
      id: 'faster_whisper', label: 'Whisper', tier: 'local', available: true, target_id: 'local',
      transcription_options: { language: true, vad: true, model_size: true },
      diarization: { supported: false, mode: 'none' },
      targets: [{
        target: 'local', target_id: 'local', available: true,
        transcription_options: { language: true, vad: true, model_size: true },
        diarization: { supported: false, mode: 'none' },
        sizes: ['tiny', 'base', 'small', 'medium'],
      }],
    },
    {
      id: 'parakeet-tdt', label: 'Parakeet', tier: 'local', available: true, target_id: 'local-onnx',
      transcription_options: { language: false, vad: true, model_size: false },
      diarization: { supported: false, mode: 'none' },
      targets: [{
        target: 'local-onnx', target_id: 'local-onnx', available: true,
        transcription_options: { language: false, vad: true, model_size: false },
        diarization: { supported: false, mode: 'none' },
      }],
    },
  ],
  'map.find_topic_sections': [
    { id: 'deep_tiling', label: 'Semantic windows', recommended: true, tier: 'local', available: true },
    { id: 'texttiling', label: 'Lexical cohesion', tier: 'local', available: true },
  ],
};

const TABS = [
  {
    prefix: 'ocr-compare',
    action: 'media.ocr',
    chipTestId: 'ocr-compare-variant-chip',
    addTestId: 'ocr-compare-add-engine',
    render: () => render(<OcrCompareTab onSessionChange={() => undefined} />),
    file: () => new File(['pixels'], 'receipt.png', { type: 'image/png' }),
    estimate: () => vi.spyOn(api, 'estimateOcrScratch'),
  },
  {
    prefix: 'transcribe-compare',
    action: 'media.transcribe',
    chipTestId: 'transcribe-compare-engine-chip',
    addTestId: 'transcribe-compare-add-engine',
    render: () => render(<TranscribeCompareTab onSessionChange={() => undefined} />),
    file: () => new File(['audio'], 'clip.wav', { type: 'audio/wav' }),
    estimate: () => vi.spyOn(api, 'estimateTranscribeScratch'),
  },
  {
    prefix: 'topic-compare',
    action: 'map.find_topic_sections',
    chipTestId: 'topic-compare-variant-chip',
    addTestId: 'topic-compare-add-variant',
    render: () => render(<TopicSegmentationCompareTab onSessionChange={() => undefined} />),
    file: () => new File(['one\ntwo'], 'sample.txt', { type: 'text/plain' }),
    estimate: null,
  },
] as const;

function chipsRow(prefix: string): string {
  return screen.getByTestId(`${prefix}-chips-row`).outerHTML
    .replace(/col-\d+/g, 'col-#')
    .replace(/(«|:)r[0-9a-z]+(»|:)/g, '$1r#$2');
}

async function catalogChips(tab: (typeof TABS)[number], count = 2) {
  await waitFor(() => expect(screen.getAllByTestId(tab.chipTestId)
    .map((chip) => chip.querySelector('.ocr-compare-variant-name')?.textContent))
    .toEqual(CATALOG[tab.action].slice(0, count).map((engine) => engine.label)));
}

async function settledPopover(prefix: string) {
  const popover = await screen.findByTestId(`${prefix}-configure`);
  await waitFor(() => expect(within(popover).getByTestId(`${prefix}-configure-engine`)).toBeVisible());
  await new Promise((resolve) => setTimeout(resolve, 20));
  return popover;
}

beforeEach(() => {
  vi.spyOn(selectorChoicesApi, 'getSelectorChoices').mockImplementation(async (_pid, query) =>
    mediaChoices(query, Object.values(CATALOG).flat().map((engine) => engine.id)));
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:upload');
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
  vi.spyOn(api, 'listActionCatalog').mockResolvedValue({
    actions: Object.entries(CATALOG).map(([kind, engines]) => ({ kind, ui_hints: { engines } })),
  } as never);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe.each(TABS)('$prefix variant chips markup', (tab) => {
  it('renders the idle chip row', async () => {
    tab.render();
    await catalogChips(tab);
    expect(chipsRow(tab.prefix)).toMatchSnapshot();
  });

  it('renders the chip row with Configure open on a chosen engine', async () => {
    const user = userEvent.setup();
    tab.render();
    await catalogChips(tab);
    await user.click(screen.getAllByTestId(`${tab.prefix}-variant-gear`)[1]);
    await settledPopover(tab.prefix);
    expect(chipsRow(tab.prefix)).toMatchSnapshot();
  });

  it('renders the chip row with a newly added variant being configured', async () => {
    const user = userEvent.setup();
    tab.render();
    await catalogChips(tab);
    await user.click(screen.getByTestId(tab.addTestId));
    await settledPopover(tab.prefix);
    expect(screen.getAllByTestId(tab.chipTestId)).toHaveLength(3);
    expect(chipsRow(tab.prefix)).toMatchSnapshot();
  });

  it('renders the chip row while a comparison is preparing', async () => {
    const user = userEvent.setup();
    if (tab.estimate) tab.estimate().mockImplementation(() => new Promise(() => undefined));
    else vi.spyOn(api, 'compareTopicSegmentationScratch').mockImplementation(() => new Promise(() => undefined));
    tab.render();
    await catalogChips(tab);
    await user.upload(screen.getByTestId(`${tab.prefix}-file-input`), tab.file());
    await waitFor(() => expect(screen.getByTestId(`${tab.prefix}-run`)).toBeEnabled());
    await user.click(screen.getByTestId(`${tab.prefix}-run`));
    await waitFor(() => expect(screen.getByTestId(`${tab.prefix}-run`)).toHaveAttribute('aria-busy', 'true'));
    expect(chipsRow(tab.prefix)).toMatchSnapshot();
  });

  it('renders the chip row with Configure open on a failed variant', async () => {
    const user = userEvent.setup();
    if (tab.estimate) tab.estimate().mockRejectedValue(new Error('fixture estimate failed'));
    else vi.spyOn(api, 'compareTopicSegmentationScratch').mockRejectedValue(new Error('fixture lane failed'));
    tab.render();
    await catalogChips(tab);
    await user.upload(screen.getByTestId(`${tab.prefix}-file-input`), tab.file());
    await waitFor(() => expect(screen.getByTestId(`${tab.prefix}-run`)).toBeEnabled());
    await user.click(screen.getByTestId(`${tab.prefix}-run`));
    await waitFor(() => expect(screen.getAllByTestId(tab.chipTestId)
      .every((chip) => chip.getAttribute('data-pip') === 'error')).toBe(true));
    await user.click(screen.getAllByTestId(`${tab.prefix}-variant-gear`)[0]);
    const popover = await settledPopover(tab.prefix);
    expect(within(popover).getByTestId(`${tab.prefix}-configure-error`)).toBeVisible();
    expect(chipsRow(tab.prefix)).toMatchSnapshot();
  });
});

describe('variant chip gear and body clicks', () => {
  it.each(['ocr-compare', 'transcribe-compare'])('%s gear and body both toggle Configure', async (prefix) => {
    const user = userEvent.setup();
    const tab = TABS.find((candidate) => candidate.prefix === prefix)!;
    tab.render();
    await catalogChips(tab);
    await user.click(screen.getAllByTestId(`${prefix}-variant-gear`)[0]);
    expect(screen.getByTestId(`${prefix}-configure`)).toBeVisible();
    await user.click(screen.getAllByTestId(`${prefix}-variant-gear`)[0]);
    expect(screen.queryByTestId(`${prefix}-configure`)).not.toBeInTheDocument();
    await user.click(screen.getAllByTestId(`${prefix}-variant-body`)[0]);
    expect(screen.getByTestId(`${prefix}-configure`)).toBeVisible();
    await user.click(screen.getAllByTestId(`${prefix}-variant-body`)[0]);
    expect(screen.queryByTestId(`${prefix}-configure`)).not.toBeInTheDocument();
  });

  it('topic-compare gear only opens Configure while the body toggles it', async () => {
    const user = userEvent.setup();
    TABS[2].render();
    await catalogChips(TABS[2]);
    await user.click(screen.getAllByTestId('topic-compare-variant-gear')[0]);
    expect(screen.getByTestId('topic-compare-configure')).toBeVisible();
    await user.click(screen.getAllByTestId('topic-compare-variant-gear')[0]);
    expect(screen.getByTestId('topic-compare-configure')).toBeVisible();
    await user.click(screen.getAllByTestId('topic-compare-variant-body')[0]);
    expect(screen.queryByTestId('topic-compare-configure')).not.toBeInTheDocument();
    await user.click(screen.getAllByTestId('topic-compare-variant-body')[0]);
    expect(screen.getByTestId('topic-compare-configure')).toBeVisible();
  });

  it('topic-compare disables adding a variant when no engine is available', async () => {
    vi.mocked(api.listActionCatalog).mockResolvedValue({ actions: [{ kind: 'map.find_topic_sections',
      ui_hints: { engines: CATALOG['map.find_topic_sections'].map((engine) => ({ ...engine, available: false })) },
    }] } as never);
    TABS[2].render();
    await waitFor(() => expect(screen.getAllByTestId('topic-compare-catalog-reason')).toHaveLength(2));
    expect(screen.getByTestId('topic-compare-add-variant')).toBeDisabled();
    expect(chipsRow('topic-compare')).toMatchSnapshot();
  });
});
