import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createProject, openDiscoverTab, uniqueName } from './helpers';
import { testPython } from '../support/testPython';

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();

interface SeededHealthSources {
  failingId: number;
  disabledId: number;
  neverId: number;
}

function seedHealthSources(pid: string): SeededHealthSources {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = String.raw`
import json
import sys
from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store.feeds import FeedStore

workspace, pid = sys.argv[1:3]
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    feed_store = FeedStore(project)
    failing = feed_store.add_feed(
        "Failing API source",
        kind="api_list_dicts",
        url="https://example.gov/api/items",
        config={
            "schema_version": "frisket.source.api_list_dicts.v1",
            "method": "GET",
            "url": "https://example.gov/api/items",
            "list_path": "/results",
            "item_id_path": "/id",
            "schema_policy": "additive",
        },
        schedule="@hourly",
        enabled=True,
    )
    disabled = feed_store.add_feed(
        "Disabled Court source",
        kind="courtlistener_docket",
        url="https://www.courtlistener.com/docket/123456/example/",
        config={
            "schema_version": "frisket.source.courtlistener_docket.v1",
            "docket_url": "https://www.courtlistener.com/docket/123456/example/",
            "keywords": ["injunction"],
            "include_parties": True,
            "include_documents": True,
            "recap_pdf_policy": "link_only",
        },
        schedule="@daily",
        enabled=False,
    )
    never = feed_store.add_feed(
        "Never-run YouTube source",
        kind="youtube_channel",
        url="https://www.youtube.com/@fixturechannel",
        config={
            "schema_version": "frisket.source.youtube.v1",
            "channel_url": "https://www.youtube.com/@fixturechannel",
            "max_pages_per_poll": 5,
        },
        schedule=None,
        enabled=True,
    )

    # Sparse legacy run, then a success, then two failures so health can show
    # both missing-metadata warnings and consecutive failure state.
    feed_store.record_feed_check(failing, status="ok", new_rows=1)
    success = feed_store.start_feed_check(failing)
    feed_store.finish_feed_check(
        success,
        status="ok",
        new_rows=3,
        skipped_rows=4,
        changed_rows=1,
        revisions=0,
        cursor_after=json.dumps({"cursor": "secret-body-not-returned"}),
        duration_ms=215,
        warning_count=1,
        cost_micro=4200,
        summary={"materialized_rows": 3},
    )
    first_failure = feed_store.start_feed_check(failing)
    feed_store.finish_feed_check(
        first_failure,
        status="error",
        error="provider failed token=SECRETabc",
        duration_ms=50,
        warning_count=1,
        summary={"failed": True},
    )
    second_failure = feed_store.start_feed_check(failing)
    feed_store.finish_feed_check(
        second_failure,
        status="error",
        error="provider failed authorization=Bearer SECRETdef",
        duration_ms=48,
        warning_count=1,
        summary={"failed": True},
    )
    print(json.dumps({"failingId": failing, "disabledId": disabled, "neverId": never}))
finally:
    project.close()
`;
  const output = execFileSync(testPython(), ['-c', script, workspace, pid], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
  });
  return JSON.parse(output) as SeededHealthSources;
}

test('feed health drawer shows redacted status, deltas, costs, warnings, and retry', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-source-health'));
  const seeded = seedHealthSources(pid);
  const pollPosts: Array<Record<string, unknown>> = [];

  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    if (payload.action_id === 'source.poll') {
      pollPosts.push(payload);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 'frisket.action_result.v1',
          status: 'completed',
          outputs: [
            {
              kind: 'source_run',
              name: 'source_run',
              sheet_id: null,
              ref: {
                source_run_id: 999,
                source_id: (payload.params as Record<string, unknown>).source,
                sheet_id: null,
                new_rows: 0,
                skipped_rows: 0,
                changed_rows: 0,
                revisions: 0,
                warning_count: 0,
              },
            },
          ],
          errors: [],
        }),
      });
      return;
    }
    await route.continue();
  });

  await page.goto(`/p/${pid}`);
  await openDiscoverTab(page, 'Sources');

  await page.getByTestId(`feed-health-${seeded.failingId}`).click();
  await expect(page.getByTestId('feed-health-drawer')).toBeVisible();
  const summaryTab = page.getByTestId('workbench-contribution-frisket-core-view-source-summary');
  const healthTab = page.getByTestId('workbench-contribution-frisket-core-view-source-health');
  const runsTab = page.getByTestId('workbench-contribution-frisket-core-view-source-runs');
  await expect(summaryTab).toBeVisible();
  await expect(summaryTab).toHaveAttribute('data-schema-version', 'frisket.workbench.view.v1');
  await expect(summaryTab).toHaveAttribute('data-contribution-id', 'frisket.core.view.source_summary');
  await expect(summaryTab).toHaveAttribute('data-host', 'sourceDetail');
  await expect(summaryTab).toHaveAttribute('data-mode', 'tab');
  await expect(summaryTab).toHaveAttribute('data-runtime-component-key', 'core.sourceDetail.Summary');
  await expect(summaryTab).toHaveAttribute('data-required-capabilities', /source.detail.summary/);
  await expect(healthTab).toHaveAttribute('data-runtime-component-key', 'core.sourceDetail.Health');
  await expect(healthTab).toHaveAttribute('data-host', 'sourceDetail');
  await expect(healthTab).toHaveAttribute('data-required-capabilities', /source.health.read/);
  await expect(runsTab).toHaveAttribute('data-runtime-component-key', 'core.sourceDetail.Runs');
  await expect(runsTab).toHaveAttribute('data-host', 'sourceDetail');
  await expect(runsTab).toHaveAttribute('data-required-capabilities', /source.runs.list/);
  await expect(page.getByTestId('source-detail-tab-summary')).toHaveAttribute('aria-selected', 'true');
  await page.getByTestId('source-detail-tab-health').click();
  await expect(page.getByTestId(`feed-health-status-${seeded.failingId}`)).toHaveText('failing');
  await expect(page.getByTestId(`feed-health-last-success-${seeded.failingId}`)).not.toHaveText('never');
  await expect(page.getByTestId(`feed-health-last-failure-${seeded.failingId}`)).not.toHaveText('never');
  await expect(page.getByTestId(`feed-health-consecutive-failures-${seeded.failingId}`)).toHaveText('2');
  await expect(page.getByTestId(`feed-health-deltas-${seeded.failingId}`)).toContainText('+4 new');
  await expect(page.getByTestId(`feed-health-deltas-${seeded.failingId}`)).toContainText('1 changed');
  await expect(page.getByTestId(`feed-health-deltas-${seeded.failingId}`)).toContainText('4 skipped');
  await expect(page.getByTestId(`feed-health-cost-${seeded.failingId}`)).toContainText('$0.0042');
  await page.getByTestId('source-detail-tab-runs').click();
  await expect(page.getByTestId(`feed-health-checks-${seeded.failingId}`)).toContainText('provider failed');
  await expect(page.getByTestId(`feed-health-checks-${seeded.failingId}`)).not.toContainText('SECRET');
  await expect(page.getByTestId(`feed-health-warnings-${seeded.failingId}`)).toContainText('lack v1 runtime metadata');
  await expect(page.getByTestId(`feed-health-jobs-empty-${seeded.failingId}`)).toContainText('No follow-up work');
  await expect(page.getByTestId(`feed-health-redactions-${seeded.failingId}`)).toContainText('config');
  await expect(page.getByTestId(`feed-health-redactions-${seeded.failingId}`)).toContainText('cursor');

  await page.getByTestId(`feed-health-retry-${seeded.failingId}`).click();
  await expect.poll(() => pollPosts.length).toBe(1);
  expect(pollPosts[0]).toEqual({
    action_id: 'source.poll',
    scope: { kind: 'project' },
    params: { source: seeded.failingId },
    output_names: {},
    idempotency_key: expect.any(String),
  });

  await page.getByTestId(`feed-health-open-mainView-${seeded.failingId}`).click();
  await expect(page).toHaveURL(new RegExp(`/source/${seeded.failingId}/health$`));
  const mainViewHealth = page.getByTestId('feed-health-mainView');
  await expect(mainViewHealth).toBeVisible();
  const mainViewContribution = page
    .getByTestId('workbench-region-mainView')
    .getByTestId('workbench-contribution-frisket-core-view-source-health');
  await expect(mainViewContribution).toBeVisible();
  await expect(mainViewContribution).toHaveAttribute('data-schema-version', 'frisket.workbench.view.v1');
  await expect(mainViewContribution).toHaveAttribute('data-contribution-id', 'frisket.core.view.source_health');
  await expect(mainViewContribution).toHaveAttribute('data-host', 'mainView');
  await expect(mainViewContribution).toHaveAttribute('data-mode', 'pane');
  await expect(mainViewContribution).toHaveAttribute('data-runtime-component-key', 'core.sourceDetail.Health');
  await expect(mainViewContribution).toHaveAttribute('data-required-capabilities', /source.health.read/);
  await expect(mainViewContribution).toHaveAttribute('data-required-capabilities', /source.poll/);
  await expect(mainViewContribution.getByTestId(`feed-health-status-${seeded.failingId}`)).toHaveText('failing');
  await expect(mainViewContribution.getByTestId(`feed-health-redactions-${seeded.failingId}`)).toContainText('config');
  await mainViewContribution.getByTestId(`feed-health-retry-${seeded.failingId}`).click();
  await expect.poll(() => pollPosts.length).toBe(2);

  await page.goto(`/p/${pid}/source/${seeded.failingId}/health`);
  await expect(page.getByTestId('feed-health-mainView')).toBeVisible();
  await expect(
    page
      .getByTestId('workbench-region-mainView')
      .getByTestId('workbench-contribution-frisket-core-view-source-health'),
  ).toHaveAttribute('data-host', 'mainView');

  await page.getByTestId(`feed-health-${seeded.disabledId}`).click();
  await page.getByTestId('source-detail-tab-health').click();
  await expect(page.getByTestId(`feed-health-status-${seeded.disabledId}`)).toHaveText('disabled');

  await page.getByTestId(`feed-health-${seeded.neverId}`).click();
  await page.getByTestId('source-detail-tab-health').click();
  await expect(page.getByTestId(`feed-health-status-${seeded.neverId}`)).toHaveText('never checked');
});
