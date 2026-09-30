import { expect, test, type Page } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';

const TRANSCRIPT = 'Cedar Bridge submitted the revised plan. Cedar Bridge will attend the hearing.';

function reviewBundle(sheetId: number) {
  return {
    id: '901:1',
    run_id: 901,
    row_id: 1,
    sheet_id: sheetId,
    sheet_name: 'Hearings',
    action_kind: 'map.ner',
    action_name: 'Extract entities',
    model: 'local/test',
    confidence: 0.92,
    source: { Transcript: TRANSCRIPT },
    fields: [{
      run_id: 901,
      row_id: 1,
      column_id: 2,
      column_name: 'entities',
      column_type: 'json',
      sheet_id: sheetId,
      value: [{ text: 'Cedar Bridge', type: 'organization', occurrences: [
        { start: 0, end: 12, item_index: 0 },
        { start: 41, end: 53, item_index: 1 },
      ] }],
      confidence: 0.92,
      justification: 'named organization occurrences',
      review_state: 'unreviewed',
      review_decision: null,
      review_note: null,
      role: 'field',
      chore: true,
    }],
    evidence: [],
  };
}

function nerTextPayload(sheetId: number) {
  return {
    schema_version: 'frisket.evidence_viewer.v1',
    link: {
      id: 91,
      stable_id: 'evidence-link:ner-transcript',
      export_ref: 'evidence-link:ner-transcript',
      subject_kind: 'cell_value',
      subject_ref: { kind: 'run_result', run_id: 901 },
      sheet_id: sheetId,
      row_id: 1,
      column_id: 2,
      run_id: 901,
      op_id: 1,
      receipt_id: 'receipt-ner',
      role: 'primary_support',
      status: 'active',
      confidence: 0.92,
      pinned: false,
      producer: { action_kind: 'map.ner', grounding_method: 'text_offsets' },
      stale_reason: null,
      stale_at: null,
      created_at: '2026-09-30T00:00:00Z',
      text_layer_hash_mismatch: false,
    },
    artifacts: [{
      id: 71,
      stable_id: 'source-artifact:hearing',
      export_ref: 'source-artifact:hearing',
      artifact_kind: 'text',
      media_type: 'text/plain',
      title: 'Hearing transcript',
      filename: null,
      page_count: null, duration_ms: null,
      source_url: null,
      canonical_url: null,
      source_cell: { sheet_id: sheetId, row_id: 1, column_id: 1 },
      external_ref: {},
      artifact_ref: { kind: 'source_artifact', stable_id: 'source-artifact:hearing', artifact_kind: 'text',
        media_type: 'text/plain', blob: null, source_url: null, external_ref: {} },
      metadata: {},
      text_context: {
        text: TRANSCRIPT,
        offset_unit: 'utf16_code_unit',
        ranges: [
          { span_id: 'span:cedar-first', start: 0, end: 12 },
          { span_id: 'span:cedar-second', start: 41, end: 53 },
        ],
        transcript: {
          schema_version: 'frisket.timestamped_text_context.v1',
          evidence_link_stable_id: 'evidence-link:source-audio',
          artifact_stable_id: 'source-artifact:source-audio',
          offset_unit: 'utf16_code_unit',
          segments: [
            { span_id: 'span:cedar-first', start: 0, end: 40, start_ms: 0, end_ms: 1500 },
            { span_id: 'span:cedar-second', start: 41, end: TRANSCRIPT.length, start_ms: 1500, end_ms: 4000 },
          ],
        },
      },
      spans: [
        {
          id: 1, stable_id: 'span:cedar-first', export_ref: 'span:cedar-first', span_kind: 'text',
          rank: 0, span_role: 'support', required: true, note: null, status: 'active',
          selector: { kind: 'text', char_start: 0, char_end: 12, data: { item_index: 0 } },
          quote: 'Cedar Bridge submitted the revised plan.', snippet: null, text_layer_hash: null,
          preview: {}, raw: { item_index: 0 }, warnings: [], deep_link_url: null,
          clip_url: null, run_index: null,
        },
        {
          id: 2, stable_id: 'span:cedar-second', export_ref: 'span:cedar-second', span_kind: 'text',
          rank: 1, span_role: 'support', required: true, note: null, status: 'active',
          selector: { kind: 'text', char_start: 41, char_end: 53, data: { item_index: 1 } },
          quote: 'Cedar Bridge will attend the hearing.', snippet: null, text_layer_hash: null,
          preview: {}, raw: { item_index: 1 }, warnings: [], deep_link_url: null,
          clip_url: null, run_index: null,
        },
      ],
      pages: [],
      runs: [],
    }],
    warnings: [],
  };
}

function sourceAudioPayload(sheetId: number) {
  const payload = nerTextPayload(sheetId);
  return {
    ...payload,
    link: { ...payload.link, stable_id: 'evidence-link:source-audio', role: 'source_provenance' },
    artifacts: [{
      ...payload.artifacts[0],
      stable_id: 'source-artifact:source-audio', artifact_kind: 'av', media_type: 'audio/wav',
      title: 'hearing.wav', filename: 'hearing.wav', duration_ms: 4000,
      artifact_ref: { kind: 'source_artifact', stable_id: 'source-artifact:source-audio', artifact_kind: 'av',
        media_type: 'audio/wav', blob: { hash: 'audio-blob', url: '/fixtures/hearing.wav', filename: 'hearing.wav' },
        source_url: null, external_ref: {} },
      text_context: null,
      spans: [
        { ...payload.artifacts[0].spans[0], span_kind: 'temporal', selector: { kind: 'temporal', start_ms: 0, end_ms: 1500 },
          clip_url: '/api/projects/local/evidence/spans/span:cedar-first/clip', run_index: 0 },
        { ...payload.artifacts[0].spans[1], span_kind: 'temporal', selector: { kind: 'temporal', start_ms: 1500, end_ms: 4000 },
          clip_url: '/api/projects/local/evidence/spans/span:cedar-second/clip', run_index: 0 },
      ],
      runs: [{ index: 0, start_ms: 0, end_ms: 4000, span_ids: ['span:cedar-first', 'span:cedar-second'], clip_url: null }],
    }],
  };
}

async function wireReviewRendering(page: Page, pid: string, sheetId: number) {
  await page.route(`**/api/projects/${pid}/review/count`, (route) => route.fulfill({ json: { count: 1 } }));
  await page.route(`**/api/projects/${pid}/review/bundles**`, (route) => route.fulfill({ json: {
    schema_version: 'frisket.review_bundles_page.v1', offset: 0, limit: 25, total: 1,
    has_more: false, next_offset: null, bundles: [reviewBundle(sheetId)],
  } }));
  await page.route(`**/api/projects/${pid}/cells/1/2/evidence`, (route) => route.fulfill({ json: {
    schema_version: 'frisket.cell_evidence.v1', sheet_id: sheetId, row_id: 1, column_id: 2,
    current_value_ref: { kind: 'run_result', run_id: 901 }, stale_count: 0,
    links: [{ id: 91, stable_id: 'evidence-link:ner-transcript', export_ref: 'evidence-link:ner-transcript',
      status: 'active', role: 'primary_support', evidence_kind: 'audio', span_count: 2, artifact_count: 1,
      snippet: 'Cedar Bridge', viewer_href: '/evidence/evidence-link:ner-transcript' }],
  } }));
  await page.route(`**/api/projects/${pid}/evidence/links/evidence-link:ner-transcript/viewer`, (route) =>
    route.fulfill({ json: nerTextPayload(sheetId) }));
  await page.route(`**/api/projects/${pid}/evidence/links/evidence-link:source-audio/viewer`, (route) =>
    route.fulfill({ json: sourceAudioPayload(sheetId) }));
}

test('review renders timestamped NER source and a read-only entity list', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  const pid = await createProject(page.request, uniqueName('e2e-review-rendering'));
  const sheetId = await importCsv(page.request, pid, 'hearings.csv', `Transcript\n"${TRANSCRIPT}"\n`);
  await wireReviewRendering(page, pid, sheetId);

  await openProject(page, pid, sheetId);
  await page.getByTestId('review-queue-button').click();
  const card = page.getByTestId('review-card');
  const source = card.getByTestId('review-source-panel');
  const entities = card.getByTestId('review-field-entities');
  await expect(source.getByTestId('review-citation-preview')).toBeVisible();
  await expect(source.locator('audio')).toBeVisible();

  // The reading surface preserves the server's two actual timestamp segments;
  // the browser never infers timings from the plain transcript.
  const segments = source.getByTestId('evidence-transcript-line');
  await expect(segments).toHaveCount(2);
  await expect(segments.nth(0).getByRole('button', { name: 'Play at 0:00' })).toBeVisible();
  await expect(segments.nth(1).getByRole('button', { name: 'Play at 0:01' })).toBeVisible();

  // map.ner values are a selectable entity list, not an editable JSON blob.
  const entityItems = entities.getByTestId('review-entity-item');
  await expect(entityItems).toHaveCount(1);
  await expect(entityItems.first()).toContainText('Cedar Bridge');
  await expect(entityItems.first()).toContainText('organization');
  await expect(entities.locator('textarea, input, [contenteditable="true"]')).toHaveCount(0);
  await expect(entities).not.toContainText('[{"text"');

  await entityItems.first().click();
  await expect(source.locator('[data-emphasized="true"]')).toHaveCount(2);

  const download = segments.nth(0).getByRole('link', { name: 'Download clip at 0:00' });
  await expect(download).toHaveAttribute('download', '');
  await expect(download).toHaveCSS('opacity', '0');
  await segments.nth(0).hover();
  await expect(download).not.toHaveCSS('opacity', '0');
  await download.focus();
  await expect(download).not.toHaveCSS('opacity', '0');

  const [sourceBox, entityBox] = await Promise.all([source.boundingBox(), entities.boundingBox()]);
  expect(sourceBox).not.toBeNull();
  expect(entityBox).not.toBeNull();
  expect(sourceBox!.x + sourceBox!.width).toBeLessThan(entityBox!.x);
  await page.screenshot({ path: testInfo.outputPath('review-timestamped-ner.png'), fullPage: true });
});
