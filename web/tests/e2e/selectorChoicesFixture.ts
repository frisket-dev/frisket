import type { Page } from '@playwright/test';

export type SelectorChoiceFixture = {
  choiceId: string;
  label: string;
  summary?: string;
  description?: string;
  status?: 'ready' | 'needs_setup' | 'working' | 'unavailable';
  canAuthor?: boolean;
  canRun?: boolean;
  blocker?: string | null;
  authoredSelection: Record<string, unknown>;
};

export type SelectorGroupFixture = {
  id: string;
  label: string;
  choices: SelectorChoiceFixture[];
};

function choice(fixture: SelectorChoiceFixture, currentChoiceId: string | null) {
  const unavailable = fixture.status === 'unavailable';
  return {
    active_operation: null,
    authored_selection: fixture.authoredSelection,
    blocker: fixture.blocker ? { code: 'unavailable', field: null, message: fixture.blocker } : null,
    can_author: fixture.canAuthor ?? !unavailable,
    can_run: fixture.canRun ?? !unavailable,
    choice_id: fixture.choiceId,
    description: fixture.description ?? '',
    facts: [],
    is_current: fixture.choiceId === currentChoiceId,
    is_default: false,
    label: fixture.label,
    model_card_url: null,
    processing_destination: { kind: 'local', label: 'On this computer' },
    resolved_target: null,
    setup: null,
    status: fixture.status ?? 'ready',
    summary: fixture.summary ?? '',
  };
}

export function actionSelectorResponse({
  projectId,
  actionId,
  field,
  groups,
  currentChoiceId,
  orphanedCurrent = null,
}: {
  projectId: string;
  actionId: string;
  field: string;
  groups: SelectorGroupFixture[];
  currentChoiceId: string | null;
  orphanedCurrent?: SelectorChoiceFixture | null;
}) {
  return {
    schema_version: 'frisket.selector_choices.v1',
    project_id: projectId,
    subject: { kind: 'action', action_id: actionId, field },
    depends_on: ['engine', 'model'],
    current_choice_id: currentChoiceId,
    default_choice_id: null,
    groups: groups.map((group) => ({
      group_id: group.id,
      kind: 'local',
      label: group.label,
      status: 'ready',
      choices: group.choices.map((item) => choice(item, currentChoiceId)),
    })),
    orphaned_current: orphanedCurrent ? choice(orphanedCurrent, currentChoiceId) : null,
  };
}

/** Stubs only the typed side-effect-free selector facade, leaving all action
 * catalog and run behavior on the live local app. */
export async function stubActionSelectorChoices(
  page: Page,
  projectId: string,
  respond: (request: { actionId: string; field: string; params: Record<string, unknown> }) => unknown,
): Promise<void> {
  await page.route(`**/api/projects/${projectId}/selector-choices`, async (route) => {
    const body = route.request().postDataJSON() as {
      subject?: { kind?: string; action_id?: string; field?: string; params?: Record<string, unknown> };
    };
    const subject = body.subject;
    if (subject?.kind !== 'action' || !subject.action_id || !subject.field) {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(respond({
        actionId: subject.action_id,
        field: subject.field,
        params: subject.params ?? {},
      })),
    });
  });
}
