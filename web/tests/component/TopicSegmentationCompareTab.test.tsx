// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';


import type {
  ActionCatalogPayload,
  TopicSegmentationCompareScratchResult,
  TopicSegmentationCompareVariantInput,
} from '../../src/api/types';
import { TopicSegmentationCompareTab } from '../../src/workbench/TopicSegmentationCompareTab';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';


const api = createProjectApi('test-project');
const { render } = createWorkspaceTestHarness({
  projectId: 'test-project',
  api: { projectApi: api },
});

const engines = [
  {
    id: 'deep_tiling',
    label: 'Semantic windows',
    description: 'Semantic topic changes',
    recommended: true,
    tier: 'local' as const,
    available: true,
  },
  {
    id: 'texttiling',
    label: 'Lexical cohesion',
    description: 'Lexical topic changes',
    tier: 'local' as const,
    available: true,
  },
];

const units = ['Apples grow.', 'Pears grow.', 'Rockets launch.', 'Astronauts orbit.'].map(
  (text, ordinal) => ({
    id: `unit-${String(ordinal).padStart(6, '0')}`,
    ordinal,
    text,
    speaker: null,
    start_ms: null,
    end_ms: null,
  }),
);

function catalog(): ActionCatalogPayload {
  return {
    actions: [{ kind: 'map.find_topic_sections', ui_hints: { engines } }],
  } as unknown as ActionCatalogPayload;
}

function payload(
  variant: TopicSegmentationCompareVariantInput,
  failed = false,
): TopicSegmentationCompareScratchResult {
  const semantic = variant.engine === 'deep_tiling';
  const boundary = semantic
    ? { key: 2, kind: 'span' as const, candidate_ids: ['semantic-boundary'] }
    : { key: 1, kind: 'point' as const, candidate_ids: ['lexical-boundary'] };
  const sections = semantic
    ? [
        {
          index: 0,
          unit_ids: [units[0].id, units[1].id, units[2].id],
        },
        {
          index: 1,
          unit_ids: [units[2].id, units[3].id],
        },
      ]
    : [
        {
          index: 0,
          unit_ids: [units[0].id],
        },
        {
          index: 1,
          unit_ids: [units[1].id, units[2].id, units[3].id],
        },
      ];
  return {
    schema_version: 'frisket.topic_segmentation_compare.v1',
    source: {
      scratch: true,
      filename: 'sample.txt',
      mime: 'text/plain',
      size: 40,
      source_kind: 'untimed_transcript',
      snapshot_hash: 'sha256:fixture',
      language: null,
    },
    engines,
    units,
    results: [
      {
        variant_id: variant.id,
        engine: variant.engine,
        engine_version: 'fixture-1',
        settings: variant.settings,
        status: failed ? 'failed' : 'completed',
        runtime_ms: 12,
        boundaries: [],
        canonical_boundaries: failed ? [] : [boundary],
        sections: failed ? [] : sections,
        unit_membership: failed
          ? []
          : units.map((unit) => ({
              unit_id: unit.id,
              section_indexes:
                semantic && unit.ordinal === 2
                  ? [0, 1]
                  : [sections.findIndex((section) => section.unit_ids.includes(unit.id))],
            })),
        diagnostics: {},
        warnings: [],
        errors: failed ? [{ code: 'fixture_failed', message: 'fixture lane failed' }] : [],
      },
    ],
    warnings: [],
    errors: failed
      ? [
          {
            code: 'fixture_failed',
            engine: variant.engine,
            message: 'fixture lane failed',
          },
        ]
      : [],
    cost: { billable: false },
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('TopicSegmentationCompareTab', () => {
  beforeEach(() => {
    vi.spyOn(api, 'listActionCatalog').mockResolvedValue(catalog());
  });

  it('uses only catalog variants and runs a staged canonical-boundary Diff', async () => {
    const compare = vi
      .spyOn(api, 'compareTopicSegmentationScratch')
      .mockImplementation(async (_file, input) => payload(input.variants[0]));
    const onSessionChange = vi.fn();
    const user = userEvent.setup();
    render(<TopicSegmentationCompareTab onSessionChange={onSessionChange} />);

    await waitFor(() =>
      expect(screen.getAllByTestId('topic-compare-variant-chip')).toHaveLength(2),
    );
    expect(
      screen.getAllByTestId('topic-compare-variant-chip').map((chip) =>
        chip.getAttribute('data-engine-id'),
      ),
    ).toEqual(['deep_tiling', 'texttiling']);

    await user.upload(
      screen.getByTestId('topic-compare-file-input'),
      new File([units.map((unit) => unit.text).join('\n')], 'sample.txt', {
        type: 'text/plain',
      }),
    );
    await waitFor(() => expect(screen.getByTestId('topic-compare-doc-item')).toBeVisible());
    expect(screen.getByTestId('topic-compare-run')).toHaveAttribute('data-run-state', 'pending');

    await user.click(screen.getByTestId('topic-compare-run'));
    await waitFor(() =>
      expect(screen.getByTestId('topic-compare-boundary-nav')).toHaveTextContent(
        '2 boundary positions differ',
      ),
    );

    expect(compare).toHaveBeenCalledTimes(2);
    expect(compare.mock.calls.map((call) => call[1].variants[0].engine).sort()).toEqual([
      'deep_tiling',
      'texttiling',
    ]);
    expect(compare.mock.calls.every((call) => call[1].variants[0].settings.detail === 'balanced')).toBe(
      true,
    );
    // Each side gets a symmetric amber marker: its own unique boundary and a
    // ghost marker at the other variant's unique position.
    expect(screen.getAllByTestId('topic-compare-boundary-ghost')).toHaveLength(2);
    expect(screen.getAllByText('shared context')).toHaveLength(2);

    await user.click(screen.getAllByTestId('topic-compare-vote-chip')[0]);
    await waitFor(() =>
      expect(screen.getByTestId('topic-compare-verdict')).toHaveTextContent(
        'Semantic windows ✓1',
      ),
    );
    expect(onSessionChange).toHaveBeenCalledWith(
      expect.objectContaining({ hasData: true, verdict: expect.stringContaining('✓1') }),
    );

    // Fresh results make the same button an explicit Re-run over all pairs.
    expect(screen.getByTestId('topic-compare-run')).toHaveAttribute('data-run-state', 'fresh');
    await user.click(screen.getByTestId('topic-compare-run'));
    await waitFor(() => expect(compare).toHaveBeenCalledTimes(4));
  });

  it('filters unsupported suffixes and surfaces the server parser error in each lane', async () => {
    vi.spyOn(api, 'compareTopicSegmentationScratch').mockImplementation(async (file, input) => {
      if (file.name === 'backwards.srt') {
        throw new Error('Transcript cue start times must be ordered.');
      }
      return payload(input.variants[0]);
    });
    const user = userEvent.setup();
    render(<TopicSegmentationCompareTab onSessionChange={() => {}} />);
    await waitFor(() => expect(screen.getAllByTestId('topic-compare-variant-chip')).toHaveLength(2));

    const invalid = new File(['value'], 'invalid.csv', { type: 'text/csv' });
    const backwardsSrt = new File(
      [
        '1\n00:00:02,000 --> 00:00:03,000\nLater\n\n2\n00:00:01,000 --> 00:00:02,000\nEarlier\n',
      ],
      'backwards.srt',
      { type: 'application/x-subrip' },
    );
    fireEvent.change(screen.getByTestId('topic-compare-file-input'), {
      target: { files: [invalid, backwardsSrt] },
    });

    await waitFor(() => expect(screen.getAllByTestId('topic-compare-doc-item')).toHaveLength(1));
    expect(screen.getByTestId('topic-compare-doc-item')).toHaveTextContent('backwards.srt');
    await user.click(screen.getByTestId('topic-compare-run'));
    await waitFor(() => expect(screen.getAllByRole('alert')).toHaveLength(2));
    expect(screen.getAllByRole('alert')[0]).toHaveTextContent(/start times must be ordered/i);
  });

  it('runs a single available engine in Survey on a base installation', async () => {
    vi.mocked(api.listActionCatalog).mockResolvedValue({
      actions: [
        {
          kind: 'map.find_topic_sections',
          ui_hints: {
            engines: [
              { ...engines[0], available: false, error: 'Install the semantic extra.' },
              engines[1],
            ],
          },
        },
      ],
    } as unknown as ActionCatalogPayload);
    const compare = vi
      .spyOn(api, 'compareTopicSegmentationScratch')
      .mockImplementation(async (_file, input) => payload(input.variants[0]));
    const user = userEvent.setup();
    render(<TopicSegmentationCompareTab onSessionChange={() => {}} />);

    await waitFor(() => expect(screen.getAllByTestId('topic-compare-variant-chip')).toHaveLength(1));
    expect(screen.getByTestId('topic-compare-mode-survey')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('topic-compare-mode-diff')).toBeDisabled();
    await user.upload(
      screen.getByTestId('topic-compare-file-input'),
      new File(['one\ntwo\nthree'], 'sample.txt', { type: 'text/plain' }),
    );
    await user.click(screen.getByTestId('topic-compare-run'));
    await waitFor(() => expect(screen.getAllByTestId('topic-compare-section')).toHaveLength(2));
    expect(compare).toHaveBeenCalledTimes(1);
  });

  it('isolates a failing lane and retries only that document/variant pair', async () => {
    let semanticAttempts = 0;
    const compare = vi
      .spyOn(api, 'compareTopicSegmentationScratch')
      .mockImplementation(async (_file, input) => {
        const variant = input.variants[0];
        if (variant.engine === 'deep_tiling') {
          semanticAttempts += 1;
          return payload(variant, semanticAttempts === 1);
        }
        return payload(variant);
      });
    const user = userEvent.setup();
    render(<TopicSegmentationCompareTab onSessionChange={() => {}} />);
    await waitFor(() => expect(screen.getAllByTestId('topic-compare-variant-chip')).toHaveLength(2));
    await user.upload(
      screen.getByTestId('topic-compare-file-input'),
      new File(['one\ntwo\nthree'], 'sample.txt', { type: 'text/plain' }),
    );
    await waitFor(() => screen.getByTestId('topic-compare-doc-item'));
    await user.click(screen.getByTestId('topic-compare-run'));

    await waitFor(() =>
      expect(screen.getByTestId('topic-compare-engine-retry')).toBeVisible(),
    );
    expect(screen.getAllByTestId('topic-compare-section')).toHaveLength(2);
    await user.click(screen.getByTestId('topic-compare-engine-retry'));
    await waitFor(() => expect(screen.queryByTestId('topic-compare-engine-retry')).toBeNull());
    expect(compare).toHaveBeenCalledTimes(3);
    expect(semanticAttempts).toBe(2);
  });

  it('switches three catalog variants to Survey and re-arms Diff when two remain', async () => {
    vi.mocked(api.listActionCatalog).mockResolvedValue({
      actions: [
        {
          kind: 'map.find_topic_sections',
          ui_hints: {
            engines: [
              ...engines,
              {
                id: 'fixture_third',
                label: 'Fixture third',
                tier: 'local',
                available: true,
              },
            ],
          },
        },
      ],
    } as unknown as ActionCatalogPayload);
    const user = userEvent.setup();
    render(<TopicSegmentationCompareTab onSessionChange={() => {}} />);
    await waitFor(() => expect(screen.getAllByTestId('topic-compare-variant-chip')).toHaveLength(2));

    await user.click(screen.getByTestId('topic-compare-add-variant'));
    await user.selectOptions(screen.getByTestId('topic-compare-configure-engine'), 'fixture_third');
    await waitFor(() => expect(screen.getAllByTestId('topic-compare-variant-chip')).toHaveLength(3));
    expect(screen.getByTestId('topic-compare-mode-survey')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('topic-compare-mode-diff')).toBeDisabled();

    await user.click(screen.getAllByTestId('topic-compare-variant-remove')[2]);
    expect(screen.getByTestId('topic-compare-mode-diff')).toHaveAttribute('aria-pressed', 'true');
  });

  // Engine-tier-visibility lane: tier badges on the variant chips and in the
  // shared ConfigureVariantPopover (MediaCompareShell.tsx — this suite is the
  // component-level pin for that popover across every compare tab), plus the
  // catalog's own unavailability reason rendered inline — never a bare
  // disabled option.
  describe('engine tier visibility', () => {
    it('renders a tier badge on every variant chip', async () => {
      render(<TopicSegmentationCompareTab onSessionChange={() => {}} />);
      await waitFor(() =>
        expect(screen.getAllByTestId('topic-compare-variant-chip')).toHaveLength(2),
      );
      const badges = screen.getAllByTestId('topic-compare-variant-tier');
      expect(badges).toHaveLength(2);
      for (const badge of badges) {
        expect(badge).toHaveTextContent('local');
        expect(badge).toHaveAttribute('data-tier', 'local');
      }
    });

    it('groups the configure select by tier, names the unavailable reason inline, and badges the chosen tier', async () => {
      vi.mocked(api.listActionCatalog).mockResolvedValue({
        actions: [
          {
            kind: 'map.find_topic_sections',
            ui_hints: {
              engines: [
                { ...engines[0], available: false, error: 'Install the semantic extra.' },
                engines[1],
              ],
            },
          },
        ],
      } as unknown as ActionCatalogPayload);
      const user = userEvent.setup();
      render(<TopicSegmentationCompareTab onSessionChange={() => {}} />);
      await waitFor(() =>
        expect(screen.getAllByTestId('topic-compare-variant-chip')).toHaveLength(1),
      );

      await user.click(screen.getByTestId('topic-compare-add-variant'));
      const select = screen.getByTestId('topic-compare-configure-engine') as HTMLSelectElement;
      // Options are grouped under tier optgroups (both stubs are tier local).
      expect(Array.from(select.querySelectorAll('optgroup')).map((group) => group.label)).toEqual([
        'Local',
      ]);
      // The disabled option carries the catalog's own reason, not a bare
      // "(unavailable)".
      const unavailable = select.querySelector('option[value="deep_tiling"]');
      expect(unavailable).toBeDisabled();
      expect(unavailable).toHaveTextContent('unavailable — Install the semantic extra.');

      // Choosing an engine surfaces its tier badge next to the Engine label.
      await user.selectOptions(select, 'texttiling');
      const badge = await screen.findByTestId('topic-compare-configure-engine-tier');
      expect(badge).toHaveTextContent('local');
      expect(badge).toHaveAttribute('data-tier', 'local');
    });
  });
});
