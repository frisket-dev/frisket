import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { createProject, listSheets, openDiscoverTab, openImportWorkspace, openProject, sheetData, uniqueName } from './helpers';

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();

interface WireSource {
  id: number;
  name: string;
  kind: string;
  url: string | null;
  config: Record<string, unknown>;
}

interface SeededPoll {
  runId: number;
  sheetId: number;
  rowId: number;
}

async function sources(
  page: Page,
  pid: string,
): Promise<WireSource[]> {
  const res = await page.request.get(`/api/projects/${pid}/sources`);
  expect(res.ok()).toBeTruthy();
  return res.json();
}

async function sourceByName(page: Page, pid: string, name: string): Promise<WireSource> {
  await expect.poll(async () => (await sources(page, pid)).some((source) => source.name === name)).toBeTruthy();
  const source = (await sources(page, pid)).find((candidate) => candidate.name === name);
  if (!source) throw new Error(`source ${name} was not created`);
  return source;
}

async function openFeedImport(page: Page) {
  await openImportWorkspace(page);
  await page.getByTestId('import-mode-feed').click();
}

function seedFakeSourcePoll(pid: string, sourceId: number, kind: string): SeededPoll {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = String.raw`
import json
import sys
from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store.sources import SourceStore

workspace, pid, raw_source_id, kind = sys.argv[1:5]
source_id = int(raw_source_id)
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    source_store = SourceStore(project)
    source = source_store.get_source(source_id)
    if source is None:
        raise SystemExit(f"missing source {source_id}")
    sheet_id = source["sheet_id"]
    if sheet_id is None:
        sheet_id = project.add_sheet(f"{source['name']} rows")
        source_store.update_source(source_id, sheet_id=sheet_id)
    columns = {row["name"]: row["id"] for row in project.columns(sheet_id)}
    column_types = {
        "source_item_id": "text",
        "source_id": "integer",
        "source_run_id": "integer",
        "title": "text",
        "url": "link",
        "video_id": "text",
        "channel_title": "text",
        "api_id": "text",
        "status": "text",
        "item_type": "category",
        "docket_id": "integer",
        "description": "text",
        "matched_keywords": "json",
        "source_raw": "json",
    }
    for name, column_type in column_types.items():
        if name not in columns:
            columns[name] = project.add_column(sheet_id, name, column_type)
    run_id = source_store.start_source_run(source_id)
    if kind.startswith("youtube"):
        record = {
            "source_item_id": "youtube:video:video123",
            "source_id": source_id,
            "source_run_id": run_id,
            "title": "Fixture public meeting",
            "url": "https://www.youtube.com/watch?v=video123",
            "video_id": "video123",
            "channel_title": "Fixture Channel",
            "source_raw": {"provider": "fake-youtube"},
        }
    elif kind == "api_list_dicts":
        record = {
            "source_item_id": "api:item:case-1",
            "source_id": source_id,
            "source_run_id": run_id,
            "api_id": "case-1",
            "title": "Fixture API item",
            "status": "active",
            "url": "https://example.gov/items/case-1",
            "source_raw": {"provider": "fake-api"},
        }
    else:
        record = {
            "source_item_id": "courtlistener:docket_entry:123",
            "source_id": source_id,
            "source_run_id": run_id,
            "item_type": "docket_entry",
            "docket_id": 123456,
            "description": "Fixture docket entry mentioning injunction",
            "matched_keywords": ["injunction"],
            "url": "https://www.courtlistener.com/docket/123456/example/",
            "source_raw": {"provider": "fake-courtlistener"},
        }
    row_id = project.add_rows(sheet_id, [record], columns)[0]
    source_store.finish_source_run(
        run_id,
        status="ok",
        new_rows=1,
        skipped_rows=2,
        changed_rows=1,
        revisions=0,
        cursor_after=json.dumps({"fixture_cursor": True}),
        duration_ms=120,
        warning_count=1,
        cost_micro=1234,
        summary={"materialized_rows": 1, "fixture": kind},
    )
    print(json.dumps({"runId": run_id, "sheetId": sheet_id, "rowId": row_id}))
finally:
    project.close()
`;
  const output = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, String(sourceId), kind], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
  });
  return JSON.parse(output) as SeededPoll;
}

async function createSource(
  page: Page,
  pid: string,
  kind: string,
  name: string,
  ref: string,
  options: {
    youtubeMaxPages?: string;
    youtubeMaxItems?: string;
    courtMaxEntries?: string | null;
  } = {},
): Promise<WireSource> {
  await openFeedImport(page);
  await page.getByTestId('source-kind').selectOption(kind);
  await page.getByTestId('source-name').fill(name);
  await page.getByTestId('source-url').fill(ref);
  if (kind === 'youtube_playlist' || kind === 'youtube_channel') {
    if (options.youtubeMaxPages !== undefined) {
      await page.getByTestId('source-youtube-max-pages').fill(options.youtubeMaxPages);
    }
    if (options.youtubeMaxItems !== undefined) {
      await page.getByTestId('source-youtube-max-items').fill(options.youtubeMaxItems);
    }
  }
  if (kind === 'api_list_dicts') {
    await page.getByTestId('source-api-list-path').fill('/results');
    await page.getByTestId('source-api-item-id-path').fill('/id');
    await page.getByTestId('source-api-updated-at-path').fill('/updated_at');
    await page.getByTestId('source-api-max-items').fill('25');
  }
  if (kind === 'courtlistener_docket') {
    await page.getByTestId('source-court-keywords').fill('injunction, settlement');
    const maxEntries = options.courtMaxEntries === undefined ? '250' : options.courtMaxEntries;
    if (maxEntries !== null) {
      await page.getByTestId('source-court-max-entries').fill(maxEntries);
    }
  }
  await page.getByTestId('source-create').click();
  // feed-add-populate-prompt-v1: creation swaps to a "Populate feed?" prompt
  // instead of closing outright. This suite drives its own fake seeded polls
  // (seedFakeSourcePoll) and asserts on those, so decline the real run here.
  await expect(page.getByTestId('feed-populate-prompt')).toBeVisible();
  await page.getByTestId('feed-populate-dismiss').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();
  return sourceByName(page, pid, name);
}

test('source forms preserve uncapped defaults and explicit source limits', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-source-limit-defaults'));
  const actionPosts: Array<Record<string, unknown>> = [];
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    actionPosts.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.continue();
  });

  await openProject(page, pid);
  const cases = [
    {
      kind: 'youtube_playlist',
      name: uniqueName('Uncapped playlist'),
      ref: 'https://www.youtube.com/playlist?list=PLuncapped123',
      controlIds: ['source-youtube-max-pages', 'source-youtube-max-items'],
      omittedKeys: ['max_pages_per_poll', 'max_items_per_poll'],
    },
    {
      kind: 'youtube_channel',
      name: uniqueName('Uncapped channel'),
      ref: '@uncappedchannel',
      controlIds: ['source-youtube-max-pages', 'source-youtube-max-items'],
      omittedKeys: ['max_pages_per_poll', 'max_items_per_poll'],
    },
    {
      kind: 'courtlistener_docket',
      name: uniqueName('Uncapped docket'),
      ref: 'https://www.courtlistener.com/docket/123456/uncapped/',
      controlIds: ['source-court-max-entries'],
      omittedKeys: ['max_entries_per_poll'],
    },
  ] as const;

  const created: Array<{ source: WireSource; item: (typeof cases)[number] }> = [];
  for (const item of cases) {
    const source = await createSource(page, pid, item.kind, item.name, item.ref, {
      courtMaxEntries: null,
    });
    const createAction = actionPosts.find((post) => {
      if (post.action_id !== 'source.create') return false;
      const params = post.params as Record<string, unknown>;
      return params.name === item.name;
    });
    expect(createAction).toBeTruthy();
    const createParams = createAction!.params as Record<string, unknown>;
    const createConfig = createParams.config as Record<string, unknown>;
    for (const key of item.omittedKeys) {
      expect(createConfig).not.toHaveProperty(key);
      expect(source.config).not.toHaveProperty(key);
    }
    created.push({ source, item });
  }

  await openDiscoverTab(page, 'Sources');
  for (const { source, item } of created) {
    await page.getByTestId(`source-edit-${source.id}`).click();
    for (const controlId of item.controlIds) {
      await expect(page.getByTestId(controlId)).toHaveValue('');
    }
    const editedName = `${item.name} edited`;
    await page.getByTestId('source-name').fill(editedName);
    const updateCount = actionPosts.filter((post) => post.action_id === 'source.update').length;
    await page.getByTestId('source-create').click();
    await expect.poll(
      () => actionPosts.filter((post) => post.action_id === 'source.update').length,
    ).toBe(updateCount + 1);
    const updateAction = actionPosts.filter((post) => post.action_id === 'source.update').at(-1)!;
    const updateParams = updateAction.params as Record<string, unknown>;
    const patch = updateParams.patch as Record<string, unknown>;
    const updateConfig = patch.config as Record<string, unknown>;
    expect(updateParams.source_id).toBe(source.id);
    for (const key of item.omittedKeys) {
      expect(updateConfig).not.toHaveProperty(key);
    }
    await expect.poll(async () => {
      const updated = (await sources(page, pid)).find((candidate) => candidate.id === source.id);
      return updated?.name;
    }).toBe(editedName);
  }

  const boundedPlaylistName = uniqueName('Bounded playlist');
  const boundedPlaylist = await createSource(
    page,
    pid,
    'youtube_playlist',
    boundedPlaylistName,
    'https://www.youtube.com/playlist?list=PLbounded123',
    { youtubeMaxPages: '7', youtubeMaxItems: '333' },
  );
  const boundedPlaylistAction = actionPosts.find((post) => {
    const params = post.params as Record<string, unknown>;
    return post.action_id === 'source.create' && params.name === boundedPlaylistName;
  });
  expect(boundedPlaylistAction).toBeTruthy();
  const boundedPlaylistConfig = (
    boundedPlaylistAction!.params as Record<string, unknown>
  ).config as Record<string, unknown>;
  expect(boundedPlaylistConfig).toMatchObject({
    max_pages_per_poll: 7,
    max_items_per_poll: 333,
  });
  expect(boundedPlaylist.config).toMatchObject({
    max_pages_per_poll: 7,
    max_items_per_poll: 333,
  });

  const boundedDocketName = uniqueName('Bounded docket');
  const boundedDocket = await createSource(
    page,
    pid,
    'courtlistener_docket',
    boundedDocketName,
    'https://www.courtlistener.com/docket/654321/bounded/',
  );
  const boundedDocketAction = actionPosts.find((post) => {
    const params = post.params as Record<string, unknown>;
    return post.action_id === 'source.create' && params.name === boundedDocketName;
  });
  expect(boundedDocketAction).toBeTruthy();
  const boundedDocketConfig = (
    boundedDocketAction!.params as Record<string, unknown>
  ).config as Record<string, unknown>;
  expect(boundedDocketConfig.max_entries_per_poll).toBe(250);
  expect(boundedDocket.config.max_entries_per_poll).toBe(250);
});

test('Sources UI creates, edits, and polls RSS, YouTube, API, and CourtListener through source.poll', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-investigative-sources'));
  const actionPosts: Array<Record<string, unknown>> = [];
  const mockedPolls = new Map<number, SeededPoll>();

  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    actionPosts.push(payload);
    if (payload.kind === 'source.poll') {
      const params = payload.params as Record<string, unknown>;
      const sourceId = Number(params.source_id);
      const source = (await sources(page, pid)).find((candidate) => candidate.id === sourceId);
      if (source && source.kind !== 'rss') {
        const seeded = seedFakeSourcePoll(pid, sourceId, source.kind);
        mockedPolls.set(sourceId, seeded);
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
                sheet_id: seeded.sheetId,
                ref: {
                  source_run_id: seeded.runId,
                  source_id: sourceId,
                  sheet_id: seeded.sheetId,
                  new_rows: 1,
                  skipped_rows: 2,
                  changed_rows: 1,
                  revisions: 0,
                  warning_count: 1,
                },
              },
              {
                kind: 'sheet',
                name: 'source_sheet',
                sheet_id: seeded.sheetId,
                ref: { sheet_id: seeded.sheetId },
              },
            ],
            errors: [],
          }),
        });
        return;
      }
    }
    await route.continue();
  });

  await openProject(page, pid);
  await openDiscoverTab(page, 'Sources');

  await expect(page.getByTestId('source-add-button')).toHaveCount(0);
  await openFeedImport(page);
  await page.getByTestId('source-kind').selectOption('api_list_dicts');
  await page.getByTestId('source-name').fill('Bad API');
  await page.getByTestId('source-url').fill('http://example.gov/items');
  await expect(page.getByRole('alert')).toContainText('HTTPS');
  await page.getByTestId('import-workspace-close').click();

  const rss = await createSource(page, pid, 'rss', uniqueName('RSS source'), 'http://localhost/feed.xml');
  const playlist = await createSource(
    page,
    pid,
    'youtube_playlist',
    uniqueName('Playlist source'),
    'https://www.youtube.com/playlist?list=PLfixture123',
  );
  const channel = await createSource(
    page,
    pid,
    'youtube_channel',
    uniqueName('Channel source'),
    '@fixturechannel',
  );
  const api = await createSource(
    page,
    pid,
    'api_list_dicts',
    uniqueName('API source'),
    'https://example.gov/api/items',
  );
  const court = await createSource(
    page,
    pid,
    'courtlistener_docket',
    uniqueName('Court source'),
    'https://www.courtlistener.com/docket/123456/example/',
  );

  await expect(page.getByTestId(`source-kind-${playlist.id}`)).toHaveText('YouTube playlist');
  await expect(page.getByTestId(`source-kind-${channel.id}`)).toHaveText('YouTube channel');
  await expect(page.getByTestId(`source-kind-${api.id}`)).toHaveText('API list');
  await expect(page.getByTestId(`source-kind-${court.id}`)).toHaveText('CourtListener docket');

  await page.getByTestId(`source-edit-${api.id}`).click();
  await page.getByTestId('source-api-list-path').fill('/items');
  await page.getByTestId('source-create').click();
  await expect.poll(async () => {
    const updated = (await sources(page, pid)).find((source) => source.id === api.id);
    return updated?.config.list_path;
  }).toBe('/items');

  for (const source of [playlist, channel, api, court]) {
    await page.getByTestId(`source-fetch-${source.id}`).click();
    await expect.poll(() => mockedPolls.has(source.id)).toBeTruthy();
    await expect(page.getByTestId(`source-runs-${source.id}`)).toContainText('1 changed');
    await expect(page.getByTestId(`source-runs-${source.id}`)).toContainText('2 skipped');
    await expect(page.getByTestId(`source-runs-${source.id}`)).toContainText('1 warning');
  }

  await page.getByTestId(`source-fetch-${rss.id}`).click();
  await expect.poll(() => (
    actionPosts.some((post) => post.kind === 'source.poll'
      && (post.params as Record<string, unknown>).source_id === rss.id)
  )).toBeTruthy();
  const rssPoll = actionPosts.find((post) => post.kind === 'source.poll'
    && (post.params as Record<string, unknown>).source_id === rss.id);
  expect(rssPoll).toMatchObject({
    kind: 'source.poll',
    capabilities: ['project:write', 'external:source_poll'],
    params: { source_id: rss.id },
  });
  expect(actionPosts.some((post) => post.kind === 'source.rss_fetch')).toBe(false);

  const apiPoll = actionPosts.find((post) => post.kind === 'source.poll'
    && (post.params as Record<string, unknown>).source_id === api.id);
  expect(apiPoll).toMatchObject({
    kind: 'source.poll',
    capabilities: ['project:write', 'external:source_poll'],
    params: { source_id: api.id },
  });

  const apiSheetId = mockedPolls.get(api.id)?.sheetId;
  expect(apiSheetId).toBeTruthy();
  await expect.poll(async () => (await listSheets(page.request, pid)).some((sheet) => sheet.id === apiSheetId)).toBeTruthy();
  const rows = await sheetData(page.request, pid, apiSheetId!);
  const titleColumn = rows.columns.find((column) => column.name === 'title');
  expect(titleColumn).toBeTruthy();
  expect(rows.rows[0].cells[String(titleColumn!.id)]).toBe('Fixture API item');

  await expect(page.getByTestId(`source-item-${api.id}`)).toBeVisible();
  await page.getByTestId(`source-health-${api.id}`).click();
  await expect(page.getByTestId('source-health-drawer')).toBeVisible();
  await expect(page.getByTestId(`source-health-status-${api.id}`)).toHaveText('healthy');
});
