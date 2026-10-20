import { expect, type Page } from '@playwright/test';
import { LOCAL_EDITION_DESCRIPTOR } from '../../src/editions/posture';
import { settingsSectionsFor } from '../../src/settings/openSettingsRegistry';
import { createProject, importCsv, openDiscoverTab, openProject } from '../e2e/helpers';

type Shot = (page: Page, family: string, name: string, source: string, detailSelector?: string) => Promise<void>;

const SETTINGS_SCENE_OWNERS: Record<string, string> = {
  'personal.profile': 'src/settings/PersonalSettings.tsx',
  'personal.preferences': 'src/settings/PersonalSettings.tsx',
  'personal.privacy': 'src/settings/PersonalSettings.tsx',
  'personal.aiProviders': 'src/settings/AiProviderSettings.tsx',
  'personal.diagnostics': 'src/settings/DiagnosticsSettings.tsx',
  'personal.skills': 'src/components/SkillsSettings.tsx',
  'organization.aiProviders': 'src/settings/OrganizationSettings.tsx',
  'organization.secrets': 'src/settings/OrganizationSettings.tsx',
  'organization.apiKeys': 'src/settings/OrganizationSettings.tsx',
  'organization.spend': 'src/settings/OrganizationSettings.tsx',
  'organization.skills': 'src/components/SkillsSettings.tsx',
  'organization.modelsGateway': 'src/settings/teamModelsGatewaySection.tsx; src/engine-selector/ModelsGatewaySettings.tsx',
  'project.general': 'src/settings/ProjectSettings.tsx',
  'project.access': 'src/settings/ProjectSettings.tsx',
  'project.aiProviders': 'src/settings/AiProviderSettings.tsx',
  'project.secrets': 'src/settings/ProjectIntegrationSettings.tsx',
  'project.mcpServers': 'src/settings/ProjectIntegrationSettings.tsx; src/settings/McpServersSection.tsx',
  'project.notifications': 'src/components/NotificationSettingsPanel.tsx',
  'project.plugins': 'src/settings/ProjectIntegrationSettings.tsx',
  'project.dataManagement': 'src/settings/ProjectDataManagementSettings.tsx',
};

export function settingsSceneSource(component: string): string {
  const owner = SETTINGS_SCENE_OWNERS[component];
  if (!owner) throw new Error(`Missing settings scene owner: ${component}`);
  return `src/settings/SettingsWorkspace.tsx; ${owner}`;
}

async function expandDock(page: Page) {
  const box = await page.getByTestId('bottom-dock-resize').boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  await page.mouse.move(box!.x + box!.width / 2, box!.y - 260, { steps: 8 });
  await page.mouse.up();
  await expect.poll(async () => (await page.getByTestId('bottom-dock-panel').boundingBox())?.height ?? 0).toBeGreaterThan(220);
}

async function createWorkbench(page: Page, name: string) {
  const projectId = await createProject(page.request, name);
  const sheetId = await importCsv(page.request, projectId, 'council-records.csv',
    'name,department,amount,notes\nAda,Planning,1250000,Planning hearing\nMax,Housing,250000,Housing report\nLina,Planning,750000,Council meeting\n');
  await openProject(page, projectId, sheetId);
  return { projectId, sheetId };
}

/** Real local edition only: organization/team-only settings are not impersonated. */
export async function captureSettingsScenes(page: Page, shot: Shot) {
  const { projectId } = await createWorkbench(page, 'Settings inventory');
  const sections = settingsSectionsFor(LOCAL_EDITION_DESCRIPTOR).filter(section =>
    section.scope !== 'organization' && section.visibility !== 'hosted-only');
  const content = page.getByTestId('settings-content');
  for (const section of sections) {
    await page.goto(section.routePattern.replace('{projectId}', projectId));
    await expect(content.getByRole('heading', { name: section.title, exact: true }).first()).toBeVisible();
    if (section.id === 'project.notifications') {
      await expect(page.getByTestId('notification-settings-refresh')).toBeEnabled();
    } else {
      await expect(content.locator('.content-stack').first()).toBeVisible();
    }
    await expect(content.locator('.settings-inline-status').filter({ hasText: /^Loading/ })).toHaveCount(0);
    await expect(content.getByText(/^Loading models gateway configuration…$/)).toHaveCount(0);
    if (section.section === 'diagnostics') {
      await expect(page.getByTestId('diagnose-panel-table')).toBeVisible({ timeout: 45_000 });
    }
    if (section.id === 'personal.skills') await expect(page.getByTestId('skills-editor')).toBeEnabled();
    await shot(page, 'Settings', `${section.scope}-${section.section}`, settingsSceneSource(section.component));
    // Full-page screenshots cannot reveal an inner scrolling settings pane.
    const { height, total } = await content.evaluate(element => ({ height: element.clientHeight, total: element.scrollHeight }));
    for (let offset = height * 0.8, part = 2; offset < total - 40; offset += height * 0.8, part += 1) {
      await content.evaluate((element, top) => { element.scrollTop = top; }, offset);
      await shot(page, 'Settings', `${section.scope}-${section.section}-part-${part}`, settingsSceneSource(section.component));
      if (offset + height >= total) break;
    }
    await content.evaluate(element => { element.scrollTop = 0; });

    if (section.id === 'personal.ai-providers') {
      await content.getByRole('button', { name: 'Add new key', exact: true }).click();
      await expect(content.getByLabel('Provider key', { exact: true })).toBeVisible();
      await shot(page, 'Settings', 'personal-provider-key-form', 'src/settings/AiProviderSettings.tsx; src/engine-selector/ProviderKeyForm.tsx');
      await page.getByTestId('local-endpoint-add').click();
      await expect(page.getByTestId('local-endpoint-add-form')).toBeVisible();
      await shot(page, 'Settings', 'personal-local-endpoint-form', 'src/settings/AiProviderSettings.tsx');
    }
    if (section.id === 'project.mcp-servers') {
      await content.getByRole('button', { name: 'Add MCP server', exact: true }).click();
      await expect(page.getByTestId('mcp-server-form')).toBeVisible();
      await shot(page, 'Settings', 'project-mcp-server-form', 'src/settings/McpServersSection.tsx');
    }
  }
}

async function openViews(page: Page) {
  const rail = page.getByTestId('discover-rail');
  if (await rail.isVisible()) {
    await page.getByTestId('discover-rail-icon-Views').click();
  } else {
    const tab = page.getByTestId('discover-tab-Views');
    if (await tab.isVisible()) await tab.click();
    else {
      await page.getByTestId('discover-tab-overflow').click();
      await page.getByTestId('discover-tab-menu-Views').click();
    }
  }
  await expect(page.getByTestId('views-panel')).toBeVisible();
}

/** Small real data, no remote sources, model runs, background schedules, or fabricated events. */
export async function capturePanelScenes(page: Page, shot: Shot) {
  const { projectId, sheetId } = await createWorkbench(page, 'Panels inventory');
  await openDiscoverTab(page, 'Facets');
  await expect(page.getByTestId('friendly-filters-panel')).toBeVisible();
  await expect(page.getByTestId('friendly-filters-panel').getByText('Planning', { exact: true })).toBeVisible();
  await expect(page.getByTestId('friendly-filters-panel').locator('[aria-busy="true"]')).toHaveCount(0);
  await shot(page, 'Side panels', 'filters-populated', 'src/components/FriendlyFiltersPanel.tsx');
  const planning = page.getByTestId('facet-check-department-Planning');
  await planning.check();
  await expect(page.getByTestId('facets-active-filters')).toBeVisible();
  await shot(page, 'Side panels', 'filters-active', 'src/components/FriendlyFiltersPanel.tsx');

  await openDiscoverTab(page, 'Mentions');
  await expect(page.getByTestId('mentions-extract-cta')).toBeVisible();
  await shot(page, 'Side panels', 'mentions-no-entity-column', 'src/components/MentionsPanel.tsx');

  await openDiscoverTab(page, 'Sources');
  await expect(page.getByTestId('sources-empty')).toBeVisible();
  await shot(page, 'Side panels', 'sources-empty', 'src/components/SourcesPanel.tsx; src/components/sources/SourceList.tsx');
  await page.getByTestId('sources-add-source').click();
  await expect(page.getByTestId('source-form')).toBeVisible();
  await shot(page, 'Side panels', 'source-form', 'src/components/importWorkspace/ImportWorkspaceDialog.tsx; src/components/importWorkspace/FeedSourceForm.tsx');
  await page.getByTestId('import-workspace-close').click();
  await expect(page.getByTestId('import-workspace-dialog')).toHaveCount(0);

  await openViews(page);
  await expect(page.getByTestId('saved-views-list')).toContainText('No saved views yet.');
  await shot(page, 'Side panels', 'views-empty', 'src/workspace/ViewsPanel.tsx');
  await page.getByTestId('create-saved-view').click();
  await page.getByTestId('view-name-input').fill('Planning records');
  await shot(page, 'Side panels', 'view-create', 'src/workspace/ViewsPanel.tsx');
  await page.getByTestId('save-view-button').click();
  await expect(page.getByTestId('saved-view-item')).toHaveCount(1);
  await expect(page.getByTestId('views-panel')).toHaveAttribute('aria-busy', 'false');
  await shot(page, 'Side panels', 'views-populated', 'src/workspace/ViewsPanel.tsx');

  await openDiscoverTab(page, 'Watches');
  await expect(page.getByTestId('watches-empty')).toBeVisible();
  await expect(page.getByTestId('watches-panel')).toHaveAttribute('aria-busy', 'false');
  await shot(page, 'Side panels', 'watches-empty', 'src/components/WatchesPanel.tsx');
  await page.getByTestId('new-watch-from-current-grid').click();
  await expect(page.getByTestId('watch-create-composer')).toBeVisible();
  await shot(page, 'Side panels', 'watch-create', 'src/components/WatchesPanel.tsx');

  await openDiscoverTab(page, 'Notifications');
  await expect(page.getByTestId('notifications-panel')).toContainText('No notifications.');
  await shot(page, 'Side panels', 'notifications-empty', 'src/components/NotificationsPanel.tsx');

  await openDiscoverTab(page, 'Embeddings');
  await expect(page.getByTestId('embeddings-empty')).toBeVisible();
  await shot(page, 'Side panels', 'embeddings-empty', 'src/components/EmbeddingsPanel.tsx');
  await page.getByTestId('embeddings-add-button').click();
  await expect(page.getByTestId('embeddings-create-modal')).toBeVisible();
  await expect(page.getByTestId('embedding-source-col-notes')).toBeVisible();
  await expect(page.getByTestId('embeddings-create-modal').locator('.engine-selector-field-loading')).toHaveCount(0);
  await shot(page, 'Side panels', 'embedding-index-create', 'src/components/embeddings/CreateEmbeddingIndexDialog.tsx');

  // Navigation closes the unsaved embedding modal without starting model work.
  await openProject(page, projectId, sheetId);
  await expandDock(page);
  for (const tab of ['jobs', 'errors', 'history', 'lineage']) {
    await page.getByTestId(`bottom-dock-tab-${tab}`).click();
    const dock = page.getByTestId('bottom-dock-panel');
    await expect(dock).toBeVisible();
    await expect(dock.getByText(/^Loading (jobs|errors)\.\.\.$/)).toHaveCount(0);
    if (tab === 'history') {
      await expect(page.getByTestId('history-list')).toBeVisible();
      await expect(page.locator('[data-testid^="history-step-op-"]').first()).toBeVisible();
    }
    if (tab === 'lineage') await expect(page.getByTestId('lineage-panel')).toBeVisible();
    await shot(page, 'Bottom dock', tab, 'src/workbench/WorkbenchBottomDock.tsx; components/HistoryPanel.tsx');
  }
}

/** Explicit synthetic API states, using the same wire fixtures as the E2E suite. */
export async function capturePopulatedPanelScenes(page: Page, shot: Shot) {
  const { embeddingIndexFixture, embeddingIndexListFixture } = await import('../support/embeddingIndexFixtures');
  const { projectId, sheetId } = await createWorkbench(page, 'Populated panel inventory');
  const run = await page.request.post(`/api/projects/${projectId}/actions/v1/run`, { data: {
    action_id: 'map.template', scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: { template: { text: '{{department}}: {{notes}}' } },
    output_names: { rendered: 'summary_label' }, idempotency_key: `inventory-lineage-${projectId}`,
  } });
  expect(run.ok()).toBeTruthy();
  expect((await run.json()).status).toBe('completed');
  const timestamp = '2026-10-19T12:00:00Z';
  // Wire shape: embeddings-queue-progress.spec.ts; failure error is a string
  // per ActionJob in contracts/http/models.py, not an invented error envelope.
  const jobs = [
    { id: 913, status: 'failed', name: 'Refresh embedding index', error: 'Model server became unavailable while refreshing the index.' },
    { id: 914, status: 'done', name: 'Refresh embedding index', error: null },
  ].map(item => ({
    schema_version: 'frisket.job.v1', project_id: projectId, job_id: item.id,
    kind: 'action.run', run_id: null, receipt_id: null, payload_ref: {},
    status: item.status, action_kind: 'embedding.index_refresh', action_name: item.name,
    attempts: 1, max_attempts: 3,
    lease: { locked_by: null, locked_at: null, lease_expires_at: null, lease_expired: false },
    timing: { created_at: timestamp, started_at: timestamp, finished_at: timestamp },
    error: item.error, result_summary: { status: item.status, refreshed: item.error ? 1 : 3 },
  }));
  await page.route(new RegExp(`/api/projects/${projectId}/actions/jobs(\\?[^/]*)?$`), route =>
    route.fulfill({ json: { schema_version: 'frisket.job_list.v1', project_id: projectId, jobs } }));
  for (const job of jobs) await page.route(`**/api/projects/${projectId}/actions/jobs/${job.job_id}`, route => route.fulfill({ json: job }));

  // Wire shape reused from panel-reveal-over-drawer.spec.ts.
  const notification = {
    id: 501, source_kind: 'watch', source_ref: { watch_id: 7 }, source_event_ids: [901],
    event_count: 1, event_kinds: ['watch_run_completed'], title: 'Planning records watch',
    summary: '2 new planning records match this watch.', severity: 'info',
    deep_link: { kind: 'watch_run', watch_id: 7, run_id: 55, event_ids: [901] },
    created_at: timestamp, updated_at: timestamp, state: 'unseen',
    seen_at: null, read_at: null, acknowledged_at: null, acknowledged_by: null,
  };
  await page.route(`**/api/projects/${projectId}/notifications/summary`, route => route.fulfill({ json: {
    schema_version: 'frisket.notifications_summary.v1', total: 1, unseen: 1, seen: 0,
    read: 0, acknowledged: 0, by_severity: { info: 1 }, by_source_kind: { watch: 1 }, by_source_ref: [],
  } }));
  await page.route(new RegExp(`/api/projects/${projectId}/notifications(\\?[^/]*)?$`), route => route.fulfill({ json: {
    schema_version: 'frisket.notifications_page.v1', order: 'desc', offset: 0, limit: 20,
    total: 1, has_more: false, next_offset: null, notifications: [notification],
  } }));
  const index = embeddingIndexFixture({
    index_id: 'inventory_notes', name: 'Council notes', sheet_id: sheetId, space_id: 'inventory_notes',
    modality: 'text', provider_id: 'fastembed', model_id: 'paraphrase-MiniLM',
    source_columns: ['notes'], total_items: 3, ready_items: 2, stale_items: 1,
    last_refreshed_at: timestamp, dimension: 384, distance_metric: 'cosine',
  });
  await page.route(`**/api/projects/${projectId}/embeddings/v1/indexes*`, route =>
    route.fulfill({ json: embeddingIndexListFixture(sheetId, [index]) }));

  await openProject(page, projectId, sheetId);
  await expandDock(page);
  for (const tab of ['jobs', 'errors']) {
    const button = page.getByTestId(`bottom-dock-tab-${tab}`);
    if (await button.getAttribute('aria-selected') !== 'true') await button.click();
    await page.getByTestId('bottom-dock-job-913').click();
    await expect(page.locator('aside.bottom-dock-detail')).toContainText(jobs[0].error!);
    await shot(page, 'Bottom dock', `${tab}-failed-details`, 'synthetic API state; real component: src/workbench/WorkbenchBottomDock.tsx');
  }
  await openDiscoverTab(page, 'Notifications');
  await expect(page.getByTestId('notification-item')).toHaveCount(1);
  await shot(page, 'Side panels', 'notification-unread', 'synthetic API state; real component: src/components/NotificationsPanel.tsx');
  await openDiscoverTab(page, 'Embeddings');
  await expect(page.getByTestId('embedding-index-inventory_notes')).toBeVisible();
  await shot(page, 'Side panels', 'embedding-index-stale', 'synthetic API state; real component: src/components/embeddings/EmbeddingIndexCard.tsx');
  await page.getByTestId('bottom-dock-tab-lineage').click();
  await expect(page.getByTestId('lineage-panel')).toBeVisible();
  await expect(page.locator('[data-testid^="lineage-node-"]').first()).toBeVisible();
  await shot(page, 'Bottom dock', 'lineage-import-and-template', 'real local import and model-free map.template; src/workbench/LineagePanel.tsx');
}

/** Synthetic read-only card states. No source polling, watch evaluation or delivery occurs. */
export async function capturePanelCardScenes(page: Page, shot: Shot) {
  const { projectId, sheetId } = await createWorkbench(page, 'Synthetic card contents inventory');
  const timestamp = '2026-10-19T12:00:00Z';
  await page.clock.setFixedTime(new Date('2026-10-19T12:05:00Z'));
  const sourceId = 701;
  const watchId = 702;
  const pageInfo = { order: 'desc', offset: 0, limit: 20, total: 1, has_more: false, next_offset: null };

  // Wire shapes: projectSourceReadsHttpContracts.test.ts. The .invalid URL
  // deliberately exercises long-reference wrapping and is never requested.
  const source = {
    id: sourceId, name: 'Synthetic fixture: city planning and housing committee public-records archive with long source title',
    kind: 'rss', url: 'https://records.invalid/council/planning-and-housing/public-records/2026/committee-reports-and-budget-amendments/feed.xml',
    config: {}, sheet_id: sheetId, schedule: null, enabled: true, cursor: null,
    last_checked_at: timestamp, last_status: 'error', new_rows_total: 42, created_at: timestamp,
  };
  const sourceRun = {
    id: 711, source_id: sourceId, op_id: null, receipt_id: null, status: 'error',
    new_rows: 0, skipped_rows: 2, changed_rows: 0, revisions: 0, duration_ms: 180,
    warning_count: 1, cost_micro: 0, error: 'Synthetic fixture: the archive returned an incomplete response; previously imported records remain available.',
    cursor_before: null, cursor_after: null, summary_json: '{}', started_at: timestamp, finished_at: timestamp,
  };
  const sourcePage = { schema_version: 'frisket.source_runs_page.v1', ...pageInfo };
  const health = {
    schema_version: 'frisket.source_health.v1', source: { ...source, redactions: ['config', 'cursor'] },
    summary: {
      status: 'failing', last_success_at: '2026-10-19T11:00:00Z', last_failure_at: timestamp,
      consecutive_failures: 1, new_rows_total: 42, new_rows_recent: 4, changed_rows_recent: 1,
      skipped_rows_recent: 2, revisions_recent: 1, recent_run_count: 1, last_cursor_summary: 'stored',
    },
    runs_page: sourcePage,
    runs: [{ ...sourceRun, error_summary: sourceRun.error, cursor_before_present: true, cursor_after_present: false, summary_present: true }],
    downstream_jobs: [{
      job_id: 712, kind: 'source.poll', status: 'failed', attempts: 1, max_attempts: 3,
      created_at: timestamp, started_at: timestamp, finished_at: timestamp,
      refs: { source_id: sourceId }, result_summary: {}, error_summary: 'Synthetic fixture: archive unavailable.', stalled: false,
    }],
    costs: { recent_actual_micro: 0, recent_estimated_micro: 0, basis: 'synthetic fixture; no work executed' },
    alerts: [], warnings: [{ code: 'synthetic_fixture', message: 'Synthetic fixture: one loaded poll has incomplete archive metadata.' }],
  };

  // Wire shapes: watchesHttpContracts.test.ts. Both history outcomes are
  // presentation fixtures; the real Run control is never clicked.
  const watchRun = {
    id: 721, watch_id: watchId, status: 'ok', op_cursor_before: 0, op_cursor_after: 3,
    matched_rows: 12, new_rows: 2, error: null, error_code: null,
    resolved_query_hash: 'synthetic-planning-query', resolved_query: { kind: 'fts', q: 'planning budget' },
    started_at: timestamp, finished_at: timestamp,
  };
  const watch = {
    id: watchId, name: 'Synthetic fixture: planning applications and housing budget amendments across committee reports',
    scope: 'project', sheet_id: sheetId,
    query: { kind: 'fts', q: 'planning applications housing budget amendments public committee reports', mode: 'keyword', rerank: 'off', limit: 50 },
    query_version: 'frisket.query.v1', query_hash: 'synthetic-planning-query', detection_policy: { kind: 'new_matches' },
    enabled: false, last_evaluated_op: 3, last_run_id: watchRun.id, last_status: 'ok',
    created_at: timestamp, updated_at: timestamp, latest_run: watchRun,
  };
  const watchRuns = [watchRun, {
    ...watchRun, id: 720, status: 'error', matched_rows: 0, new_rows: 0,
    error: 'Synthetic fixture: evaluation interrupted; the last completed results remain available.',
    started_at: '2026-10-19T11:00:00Z', finished_at: '2026-10-19T11:00:00Z',
  }].map(run => ({ ...run, hits: [], hits_limit: 20, hits_truncated: false }));

  // Wire shapes: notificationDeliverySettingsHttpContracts.test.ts. Only
  // display-safe config is present; no secret value or delivery is created.
  const channel = {
    id: 731, kind: 'slack', name: 'Synthetic fixture: planning and housing investigations newsroom alerts',
    enabled: true, owner_kind: 'project', config: { channel_label: '#planning-and-housing-public-records' },
    has_secret: true, created_at: timestamp, updated_at: timestamp,
  };
  const notificationRoute = {
    id: 732, name: 'Synthetic fixture: daily digest for planning and housing committee record changes',
    enabled: false, owner_kind: 'project', owner_ref: projectId, recipient_actor_id: null,
    channel_id: channel.id, source_kind: 'watch', source_ref_match: { watch_id: watchId }, event_kinds: [],
    severity_min: 'info', delivery_mode: 'digest', digest_cadence: 'daily', digest_timezone: 'UTC',
    digest_anchor_time: '09:00', template_key: null, created_at: timestamp, updated_at: timestamp,
  };
  const delivery = {
    id: 733, route_id: notificationRoute.id, channel_id: channel.id, notification_id: null,
    digest_run_id: null, delivery_kind: 'digest', dedupe_key: 'synthetic-fixture-no-delivery',
    status: 'failed', job_id: null, available_at: timestamp,
    last_error: 'Synthetic fixture: destination unavailable; this is a displayed failure, not a real delivery attempt.',
    provider_ref: null, created_at: timestamp, updated_at: timestamp, sent_at: null,
  };
  const fixtures = new Map<string, unknown>([
    ['sources', [source]],
    [`sources/${sourceId}`, { ...source, runs: [sourceRun], runs_page: { ...sourcePage, latest_run: sourceRun, latest_run_loaded: true } }],
    [`sources/${sourceId}/health`, health],
    ['watches', [watch]],
    [`watches/${watchId}/runs`, { schema_version: 'frisket.watch_runs_page.v1', ...pageInfo, total: 2, hits_limit: 20, runs: watchRuns }],
    ['notifications/summary', {
      schema_version: 'frisket.notifications_summary.v1', total: 2, unseen: 2, seen: 0, read: 0, acknowledged: 0,
      by_severity: { info: 2 }, by_source_kind: { watch: 2 },
      by_source_ref: [{ source_kind: 'watch', source_ref: { watch_id: watchId }, unseen: 2 }],
    }],
    ['notification-channels', { schema_version: 'frisket.notification_channels.v1', channels: [
      { ...channel, id: 730, kind: 'in_app', name: 'In-app', config: {}, has_secret: false }, channel,
    ] }],
    ['notification-routes', { schema_version: 'frisket.notification_routes.v1', routes: [notificationRoute] }],
    ['notification-delivery-requests', { schema_version: 'frisket.notification_delivery_requests.v1', ...pageInfo, delivery_requests: [delivery] }],
  ]);
  await page.route(`**/api/projects/${projectId}/**`, async route => {
    const request = route.request();
    const endpoint = new URL(request.url()).pathname.split(`/api/projects/${projectId}/`)[1];
    // Grid bootstrap reads distinct values through POST. Permit only that
    // receipt-free local query on our imported sheet, not action execution,
    // source polling, watch evaluation or notification delivery mutations.
    const isColumnPreview = request.method() === 'POST'
      && endpoint === 'column-values/v1/preview'
      && request.postDataJSON()?.sheet_id === sheetId;
    if (request.method() !== 'GET' && !isColumnPreview) {
      await route.abort();
      throw new Error(`Card inventory blocked mutation: ${request.method()} ${endpoint}`);
    }
    if (fixtures.has(endpoint)) await route.fulfill({ json: fixtures.get(endpoint) });
    else await route.fallback();
  });

  await openProject(page, projectId, sheetId);
  await openDiscoverTab(page, 'Sources');
  const sourceCard = page.getByTestId(`source-item-${sourceId}`);
  await expect(sourceCard).toBeVisible();
  const pollHistory = sourceCard.getByRole('button', { name: 'Poll history', exact: true });
  await expect(pollHistory).toHaveAttribute('aria-expanded', 'false');
  await pollHistory.click();
  await expect(pollHistory).toHaveAttribute('aria-expanded', 'true');
  await expect(pollHistory).toHaveAttribute('aria-controls', `source-poll-history-${sourceId}`);
  await expect(pollHistory).toHaveCSS('border-width', '0px');
  await expect(pollHistory).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
  await expect(page.getByTestId(`source-runs-${sourceId}`)).toContainText(sourceRun.error);
  await shot(page, 'Panel cards', 'source-with-poll-history', 'synthetic read-only API fixture; src/components/sources/SourceList.tsx', `[data-testid="source-item-${sourceId}"]`);
  await page.getByTestId(`source-health-${sourceId}`).click();
  await page.getByTestId('source-detail-tab-health').click();
  await expect(page.getByTestId(`source-health-status-${sourceId}`)).toHaveText('failing');
  await shot(page, 'Panel cards', 'source-health-summary', 'synthetic read-only API fixture; src/components/sources/SourceHealthDrawer.tsx; src/components/sources/SourceHealthContent.tsx', '[data-testid="source-health-drawer"]');
  await page.getByTestId('source-detail-tab-runs').click();
  await expect(page.getByTestId(`source-health-runs-${sourceId}`)).toContainText(sourceRun.error);
  await shot(page, 'Panel cards', 'source-health-run-details', 'synthetic read-only API fixture; src/components/sources/SourceHealthDrawer.tsx; src/components/sources/SourceHealthContent.tsx', '[data-testid="source-health-drawer"]');

  await openDiscoverTab(page, 'Watches');
  await expect(page.getByTestId('watch-item')).toHaveCount(1);
  await page.getByTestId('toggle-watch-runs').click();
  await expect(page.getByTestId('watch-run-history')).toContainText('evaluation interrupted');
  await expect(page.getByTestId('watch-notification-count')).toHaveText('2');
  await shot(page, 'Panel cards', 'paused-watch-with-history', 'synthetic read-only API fixture; src/components/WatchesPanel.tsx', '[data-testid="watch-item"]');

  await page.goto(`/p/${projectId}/settings/project/notifications`);
  await expect(page.getByTestId('notification-settings-refresh')).toBeEnabled();
  await expect(page.getByTestId('notification-channel-item')).toHaveCount(2);
  for (const [kind, name] of [['channel', 'notification-channels'], ['route', 'notification-route'], ['delivery-request', 'notification-delivery']]) {
    const testId = kind === 'delivery-request' ? 'notification-delivery-request' : `notification-${kind}-item`;
    const selector = `.panel-list:has(> [data-testid="${testId}"])`;
    await expect(page.getByTestId(testId).first()).toBeVisible();
    await page.locator(selector).scrollIntoViewIfNeeded();
    await shot(page, 'Panel cards', name, 'synthetic read-only API fixture; src/components/NotificationSettingsPanel.tsx', selector);
  }
}

/** Coherent entity-column fixture; no NER execution or implicit model download. */
export async function captureMentionsScenes(page: Page, shot: Shot) {
  const projectId = await createProject(page.request, 'Mention inventory');
  const entity = (text: string, fingerprint: string) => ({ text, type: 'organization', fingerprint, start: 0, end: text.length });
  const imported = await page.request.post(`/api/projects/${projectId}/actions/v1/run`, { data: {
    action_id: 'import.rows', scope: { kind: 'project' }, sheet_name: 'Filings',
    params: {
      columns: [{ name: 'body', type: 'text' }, { name: 'entities', type: 'json' }],
      rows: [
        { body: 'Acme Corp. filed a report.', entities: [entity('Acme Corp.', 'acme corp')] },
        { body: 'ACME Corporation won a contract.', entities: [entity('ACME Corporation', 'acme corp')] },
        { body: 'Zenith published its accounts.', entities: [entity('Zenith', 'zenith')] },
      ],
    }, idempotency_key: `inventory-entities-${projectId}`,
  } });
  expect(imported.ok()).toBeTruthy();
  expect((await imported.json()).status).toBe('completed');
  const listed = await page.request.get(`/api/projects/${projectId}/sheets`);
  const sheets = await listed.json();
  const sheet = sheets.find((item: { name: string }) => item.name === 'Filings');
  expect(sheet).toBeTruthy();
  const entityColumn = sheet.columns.find((column: { name: string }) => column.name === 'entities');
  expect(entityColumn).toBeTruthy();
  entityColumn.semantic_type = 'entity_mentions';
  // The only simulated metadata is the NER-owned semantic marker. Values were
  // imported as real JSON above; filtered grid rows agree with fixture counts.
  await page.route(new RegExp(`/api/projects/${projectId}/sheets$`), route => route.fulfill({ json: sheets }));
  const dataResponse = await page.request.get(`/api/projects/${projectId}/sheets/${sheet.id}/data`);
  const data = await dataResponse.json();
  data.columns.find((column: { name: string }) => column.name === 'entities').semantic_type = 'entity_mentions';
  await page.route(new RegExp(`/api/projects/${projectId}/sheets/${sheet.id}/data(\\?[^/]*)?$`), route => {
    const filtered = new URL(route.request().url()).searchParams.has('filter');
    return route.fulfill({ json: { ...data, rows: filtered ? data.rows.slice(0, 2) : data.rows, total: filtered ? 2 : 3 } });
  });
  // MentionsPanel.test.tsx grouped-form example, in the real v2 wire casing
  // from contracts/http/entity_mentions.py (the unit fixture uses mapped casing).
  await page.route(`**/api/projects/${projectId}/entity-mentions/v1/preview`, route => route.fulfill({ json: {
    schema_version: 'entity-mentions-preview.v2', sheet_id: sheet.id,
    column: { id: entityColumn.id, name: 'entities', semantic_type: 'entity_mentions' },
    coverage: { target_rows: 3, completed_rows: 3, failed_rows: 0, sheet_rows: 3, scope_kind: 'all_rows' },
    search: null, type: null, total_groups: 2, type_totals: [{ type: 'organization', total_groups: 2 }],
    limit: 25, offset: 0, items: [
      { type: 'organization', selector: { kind: 'fingerprint', fingerprint: 'acme corp' }, label: 'Acme Corp.', row_count: 2, mention_count: 2, surface_count: 2,
        surfaces: [{ text: 'Acme Corp.', row_count: 1, mention_count: 1 }, { text: 'ACME Corporation', row_count: 1, mention_count: 1 }] },
      { type: 'organization', selector: { kind: 'fingerprint', fingerprint: 'zenith' }, label: 'Zenith', row_count: 1, mention_count: 1, surface_count: 1,
        surfaces: [{ text: 'Zenith', row_count: 1, mention_count: 1 }] },
    ],
  } }));
  await openProject(page, projectId, sheet.id);
  await openDiscoverTab(page, 'Mentions');
  await expect(page.getByTestId('mentions-group-row')).toHaveCount(2);
  await page.getByRole('button', { name: 'Show the 2 forms grouped into Acme Corp.', exact: true }).click();
  await shot(page, 'Side panels', 'mentions-grouped-forms', 'synthetic API state; real component: src/components/MentionsPanel.tsx');
  await page.getByTestId('mentions-group-row').first().locator('.mentions-group-line > .facet-value-hit').click();
  await expect(page.getByTestId('mentions-active-filter')).toBeVisible();
  await shot(page, 'Side panels', 'mentions-selected-group', 'synthetic API state; real component: src/components/MentionsPanel.tsx');
}
