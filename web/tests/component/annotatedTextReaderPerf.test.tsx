// @vitest-environment jsdom
//
// D10 is "perf is measure-first": render naively, benchmark, and add render-
// block windowing only if the DOM budget fails. This is the measurement, kept
// as a test so the answer cannot quietly stop being true.
//
// The shape that matters is DOM SIZE, not partition cost. Partitioning is one
// pass over a sorted boundary list and is measured below at ~3ms for 4,000
// spans over 284k characters — irrelevant next to laying out the elements it
// produces. Fragment count is what the browser pays, and it is bounded by
// 2n+1 for n non-overlapping marks (each mark, plus the gap after it).
//
// Reference workload: a 90-minute interview transcript at ~150 wpm is ~13,500
// words / ~96k characters, and NER over it marks on the order of 1,300
// entities. That is the number the thresholds below are set from — generously,
// because a threshold that trips on a slow CI box teaches nothing.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return { ...actual, api: { ...actual.api, getTextAnnotations: vi.fn() } };
});

import { AnnotatedTextReader } from '../../src/workbench/AnnotatedTextReader';
import {
  collectMarks,
  partitionAnnotationFragments,
} from '../../src/workbench/textAnnotationModel';

import type { TextAnnotationLayer, TextAnnotations } from '../../src/api/open';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('test-project');
const getTextAnnotations = vi.spyOn(api, 'getTextAnnotations');
const mockApi = { getTextAnnotations };
const { render } = createWorkspaceTestHarness({
  projectId: 'test-project', api: { projectApi: api },
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

/** ~96k characters of plausible transcript prose — the 90-minute reference. */
const WORDS = 17_400;
const BANK = [
  'Ada', 'Lovelace', 'told', 'the', 'committee', 'that', 'Boeing', 'had',
  'reviewed', 'the', 'motion', 'and', 'carried', 'it', 'on', 'the', 'day',
];

function transcript(words: number): string {
  const out: string[] = [];
  for (let i = 0; i < words; i += 1) out.push(BANK[i % BANK.length]!);
  return out.join(' ');
}

/** One span on every occurrence of "Ada" — ~1 entity per 10 words, which is a
 *  dense but realistic NER yield for interview prose. */
function denseLayer(text: string): TextAnnotationLayer {
  const spans = [];
  let cursor = 0;
  let n = 0;
  for (;;) {
    const at = text.indexOf('Ada', cursor);
    if (at < 0) break;
    cursor = at + 3;
    n += 1;
    spans.push({
      occurrenceId: `${n}:1`,
      start: at,
      end: at + 3,
      quote: 'Ada',
      entityType: 'person',
    });
  }
  return {
    toggleKey: '7:12:entities',
    layerFamily: 'entities',
    producerKind: 'map.ner',
    producerEngine: 'gliner',
    outputColumn: { id: '12', name: 'entities' },
    positioned: true,
    spans,
    counts: { shown: spans.length, total: spans.length, invalid: 0 },
  };
}

const TEXT = transcript(WORDS);
const LAYER = denseLayer(TEXT);

function payload(): TextAnnotations {
  return {
    sheetId: '7',
    rowId: '3',
    columnId: '11',
    text: TEXT,
    contentHash: 'sha256:perf',
    layers: [LAYER],
  };
}

describe('AnnotatedTextReader — D10 measurement', () => {
  it('the reference workload is the size D10 assumed', () => {
    // If these drift the thresholds below are measuring something else, and
    // the pinned numbers stop describing this test.
    expect(TEXT.length).toBeGreaterThan(90_000);
    expect(LAYER.spans.length).toBeGreaterThan(900);
  });

  it('partitions a 90-minute transcript in single-digit milliseconds', () => {
    const marks = collectMarks([LAYER], []);
    const started = performance.now();
    const fragments = partitionAnnotationFragments(TEXT, marks);
    const elapsed = performance.now() - started;

    // Measured 2026-07-26: 1.4ms for 1,024 spans over 96,218 characters, and
    // 2.9ms for 4,000 spans over 284k. 60ms is ~40x headroom — it fails on an
    // algorithmic regression, not on a busy runner.
    expect(elapsed).toBeLessThan(60);
    // 2n+1 is the bound, and the reason no windowing is needed: a mark plus
    // the gap after it. A partition that produced more than that would mean
    // boundaries were being invented.
    expect(fragments.length).toBeLessThanOrEqual(2 * marks.length + 1);
  });

  it('renders every mark with no windowing, within the DOM budget', async () => {
    mockApi.getTextAnnotations.mockResolvedValue(payload());
    const started = performance.now();
    render(
      <AnnotatedTextReader
        rowId="3"
        columnId="11"
        title="90-minute interview"
        disabledToggleKeys={[]}
        onSetDisabledToggleKeys={() => {}}
        onOpenMention={() => {}}
        onOpenDetail={() => {}}
        canOpenDetail
        optionsOpen={false}
        onToggleOptions={() => {}}
        optionsPopover={null}
        selectionCount={0}
      />,
    );
    const body = await screen.findByTestId('annotated-text-content');
    const elapsed = performance.now() - started;

    // Every mark is in the DOM: D10's decision was to render naively and
    // measure, and this is the assertion that we did NOT quietly window.
    expect(body.querySelectorAll('[data-testid="annotation-mark"]')).toHaveLength(
      LAYER.spans.length,
    );
    // Measured 2026-07-26: 2,048 elements, 276ms to render in jsdom (a browser
    // is much faster and the live check confirms it). An ordinary long page,
    // not a virtualization case — so D10's windowing stays unbuilt. The time
    // budget is loose and the element count, which is what actually decides
    // this, is tight.
    expect(body.childElementCount).toBeLessThan(4_000);
    expect(elapsed).toBeLessThan(5_000);
  });
});
