import type { Page } from '@playwright/test';
import type {
  HttpSelectorChoicesQuery,
  HttpSelectorChoicesResponse,
  SelectorSubject,
} from '../../src/api/selectorChoices';

// The Provider / model selector asks the server which embedding models a
// project can use. These choices stand in for that answer where no embedder
// is installed: one local model, one remote model without a key, and one
// remote model that is ready.

const EMBEDDING_CHOICES = [{
  choiceId: 'fastembed-paraphrase-MiniLM',
  groupId: 'local',
  groupLabel: 'Local',
  groupKind: 'local',
  label: 'Local text (fastembed)',
  summary: 'Runs on this computer',
  provider: 'fastembed',
  model: 'paraphrase-MiniLM',
  status: 'ready',
  canAuthor: true,
  canRun: true,
  blocker: null,
  destination: { kind: 'local', label: 'On this computer' },
}, {
  choiceId: 'openai-text-embedding-3-small',
  groupId: 'openai',
  groupLabel: 'OpenAI',
  groupKind: 'provider',
  label: 'OpenAI text-embedding-3-small',
  summary: 'Needs setup',
  provider: 'openai',
  model: 'text-embedding-3-small',
  status: 'unavailable',
  canAuthor: false,
  canRun: false,
  blocker: 'missing OPENAI_API_KEY',
  destination: { kind: 'external', label: 'OpenAI' },
}, {
  choiceId: 'cohere-embed-v3',
  groupId: 'cohere',
  groupLabel: 'Cohere',
  groupKind: 'provider',
  label: 'Cohere embed-v3',
  summary: 'Remote embedding',
  provider: 'cohere',
  model: 'embed-v3',
  status: 'ready',
  canAuthor: true,
  canRun: true,
  blocker: null,
  destination: { kind: 'external', label: 'Cohere' },
}] as const;

function embeddingSelectorResponse(
  pid: string,
  subject: Extract<SelectorSubject, { kind: 'embedding' }>,
): HttpSelectorChoicesResponse {
  const currentChoice = EMBEDDING_CHOICES.find((choice) => (
    choice.provider === subject.provider && choice.model === subject.model
  )) ?? EMBEDDING_CHOICES[0];
  return {
    schema_version: 'frisket.selector_choices.v3',
    project_id: pid,
    subject: {
      kind: 'embedding',
      modality: subject.modality ?? null,
      source_column_type: subject.source_column_type ?? null,
    },
    depends_on: [],
    current_choice_id: currentChoice.choiceId,
    default_choice_id: EMBEDDING_CHOICES[0].choiceId,
    groups: [...new Set(EMBEDDING_CHOICES.map((choice) => choice.groupId))].map((groupId) => {
      const groupChoices = EMBEDDING_CHOICES.filter((choice) => choice.groupId === groupId);
      const group = groupChoices[0];
      return {
        group_id: group.groupId,
        kind: group.groupKind,
        label: group.groupLabel,
        status: group.status,
        choices: groupChoices.map((choice) => ({
          choice_id: choice.choiceId,
          label: choice.label,
          summary: choice.summary,
          description: '',
          model_card_url: null,
          authored_selection: { kind: 'embedding', provider: choice.provider, model: choice.model },
          resolved_target: null,
          processing_destination: choice.destination,
          execution_route: null,
          route_selection: null,
          facts: [],
          status: choice.status,
          can_author: choice.canAuthor,
          can_run: choice.canRun,
          blocker: choice.blocker ? { code: 'unavailable', field: null, message: choice.blocker } : null,
          setup: null,
          configuration_actions: [],
          active_operation: null,
          is_default: choice.choiceId === EMBEDDING_CHOICES[0].choiceId,
          is_current: choice.choiceId === currentChoice.choiceId,
        })),
      };
    }),
    orphaned_current: null,
    cloud_setup: null,
  };
}

/** Answer the embedding selector's queries with EMBEDDING_CHOICES; every
 *  other selector subject still reaches the server. */
export async function stubEmbeddingSelector(page: Page, pid: string): Promise<void> {
  await page.route(`**/api/projects/${pid}/selector-choices`, async (route) => {
    const body = route.request().postDataJSON() as HttpSelectorChoicesQuery;
    if (body.subject.kind !== 'embedding') {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(embeddingSelectorResponse(pid, body.subject)),
    });
  });
}

/** Pick one of EMBEDDING_CHOICES in the create dialog's selector. */
export async function chooseEmbeddingModel(
  page: Page,
  choiceId: (typeof EMBEDDING_CHOICES)[number]['choiceId'],
): Promise<void> {
  await page.getByTestId('embedding-create-panel-source')
    .locator('.engine-selector__trigger').click();
  await page.getByTestId('engine-selector-dialog')
    .locator(`[data-engine-selector-choice="${choiceId}"]`).click();
}
