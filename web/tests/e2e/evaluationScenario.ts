import { expect, type Page } from '@playwright/test';
import { createProject, importCsv, primeMapRowsReplies, runAndWait, sheetColumns, sheetData, setProjectModelRoute, uniqueName } from './helpers';
import { exposeReplayModel, prepareOwnModelReplay, REPLAY_MODEL } from './replayModelScenario';
import { actionSelectorResponse, stubActionSelectorChoices } from './selectorChoicesFixture';

const ALTERNATE_REPLAY_MODEL = 'anthropic/claude-haiku-4-5';

export const EVALUATION_ROW_COUNT = 38;
const EVALUATION_FIELDS = [
  { name: 'plaintiff', type: 'text', description: 'Name of the plaintiff in the public notice.' },
  { name: 'amount', type: 'text', description: 'Amount awarded in the operative public order.' },
  { name: 'parties', type: 'list', description: 'List of parties named in the public notice.' },
  { name: 'summary', type: 'text', description: 'Public disposition narrative.' },
];
const longSummary = 'The operative public order awards the stated amount. ' + 'Full public reasoning remains available in the export. '.repeat(1400);
export const EVALUATION_EXPECTED = Array.from({ length: EVALUATION_ROW_COUNT }, (_, index) => ({
  county: index < 19 ? 'North' : 'South',
  case_id: `CV-${String(index % 19 + 1).padStart(3, '0')}`,
  plaintiff: `Reporter ${index + 1}`,
  amount: index === 36 ? '' : index === 37 ? '[empty]' : `${9000 + index}.00`,
  parties: [`Reporter ${index + 1}`, `Agency ${index + 1}`],
  summary: index === 0 ? longSummary : `Operative award in public notice ${index + 1}.`,
}));
export const EVALUATION_VARIANTS = ['A', 'B', 'C', 'D', 'E'].map((key, variantIndex) => ({
  key,
  context: `Public notices; evaluation fixture variant ${key}. Report the operative order.`,
  rows: EVALUATION_EXPECTED.map((row, index) => ({
    plaintiff: variantIndex === 0 && index % 4 === 0 ? 'Unknown plaintiff'
      : variantIndex === 1 && index % 5 === 0 ? 'Unknown plaintiff'
        : variantIndex === 2 && index % 7 === 0 ? 'Unknown plaintiff' : row.plaintiff,
    amount: index === 37 ? '' : variantIndex === 0 ? `$${9000 + index}` : `${9000 + index}.00`,
    parties: variantIndex === 0 ? [...row.parties].reverse() : row.parties,
    summary: ['A', 'B', 'E'].includes(key) ? 'Notice entered.' : row.summary,
  })),
}));
function csv(headers: string[], rows: unknown[][]): string {
  return [headers, ...rows].map(row => row.map(value => `"${String(value ?? '').replaceAll('"', '""')}"`).join(',')).join('\n') + '\n';
}
const EVALUATION_TARGET_CSV = csv(['county', 'case_id', 'case_text'], EVALUATION_EXPECTED.map((row, index) => [
  row.county, row.case_id, `Public notice ${index + 1}: ${row.plaintiff} v Agency ${index + 1}. Operative award ${9000 + index}.`,
]));
const EVALUATION_EXPECTED_CSV = csv(['county', 'case_id', ...EVALUATION_FIELDS.map(field => field.name)], [
  ...EVALUATION_EXPECTED.map(row => [row.county, row.case_id, row.plaintiff, row.amount, JSON.stringify(row.parties), row.summary]),
  ['West', 'UNMATCHED-1', 'Unmatched answer 1', '100.00', '[]', 'Retained in expected sheet.'],
  ['West', 'UNMATCHED-2', 'Unmatched answer 2', '200.00', '[]', 'Retained in expected sheet.'],
]);
function action(sheetId: number, context: string, model = REPLAY_MODEL) {
  return {
    action_id: 'map.extract', scope: { kind: 'sheet_rows' as const, sheet_id: sheetId },
    params: { source: ['case_text'], engine: 'llm', model,
      instruction: 'Extract the requested public docket facts.', context, fields: EVALUATION_FIELDS,
      include_confidence: false, grounding: { enabled: false } },
    output_names: Object.fromEntries(EVALUATION_FIELDS.map(field => [field.name, field.name])),
    idempotency_key: `evaluation-baseline:${sheetId}`,
  };
}
/** Prime exact plan keys, never call a provider. Product HTTP/worker paths remain real. */
function primeEvaluationReplies(projectId: string, sheetId: number, alternateModel: boolean): void {
  const variants = alternateModel
    ? [...EVALUATION_VARIANTS, { ...EVALUATION_VARIANTS[3], context: EVALUATION_VARIANTS[0].context, model: ALTERNATE_REPLAY_MODEL }]
    : EVALUATION_VARIANTS;
  for (const variant of variants) {
    const model = 'model' in variant ? variant.model : REPLAY_MODEL;
    primeMapRowsReplies(projectId, action(sheetId, variant.context, model), variant.rows);
  }
}
export async function createEvaluationScenario(page: Page, options: { alternateModel?: boolean } = {}) {
  const projectId = await createProject(page.request, uniqueName('e2e-expected-answers'));
  await prepareOwnModelReplay(page.request, projectId);
  if (options.alternateModel) {
    await setProjectModelRoute(page.request, projectId, ALTERNATE_REPLAY_MODEL, 'own');
    await stubActionSelectorChoices(page, projectId, ({ actionId, field, params }) => {
      const models = [{ id: REPLAY_MODEL, label: 'Gemini 3.5 Flash-Lite', provider: 'gemini' },
        { id: ALTERNATE_REPLAY_MODEL, label: 'Claude Haiku 4.5', provider: 'anthropic' }];
      const groups = models.map(model => ({ id: model.provider, label: model.provider, choices: [{
        choiceId: model.id, label: model.label, summary: 'Strict replay fixture',
        authoredSelection: field === 'engine' ? { kind: 'engine_model', engine: 'llm', model: model.id }
          : { kind: 'model', model: model.id },
      }] }));
      return actionSelectorResponse({ projectId, actionId, field, groups,
        currentChoiceId: typeof params.model === 'string' ? params.model : null });
    });
  } else await exposeReplayModel(page, projectId);
  const targetSheetId = await importCsv(page.request, projectId, 'public-notices.csv', EVALUATION_TARGET_CSV);
  const expectedSheetId = await importCsv(page.request, projectId, 'verified-fields.csv', EVALUATION_EXPECTED_CSV);
  primeEvaluationReplies(projectId, targetSheetId, options.alternateModel ?? false);
  const baselineRunId = await runAndWait(page.request, projectId, action(targetSheetId, EVALUATION_VARIANTS[0].context));
  const [targetColumns, expectedColumns, data] = await Promise.all([
    sheetColumns(page.request, projectId, targetSheetId), sheetColumns(page.request, projectId, expectedSheetId),
    sheetData(page.request, projectId, targetSheetId, 0, EVALUATION_ROW_COUNT),
  ]);
  expect(data.rows).toHaveLength(EVALUATION_ROW_COUNT);
  return { projectId, targetSheetId, expectedSheetId, baselineRunId, targetColumns, expectedColumns, rowIds: data.rows.map(row => row.id) };
}
