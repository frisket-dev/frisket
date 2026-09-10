// Reviewed clustering posts one typed request with nested review intent and
// whole-column scope. Entity materialization remains a separate action.

import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  revealRibbonAction,
  uniqueName,
} from './helpers';

type PostedAction = {
  action_id: string;
  scope: { kind: string; sheet_id: number; row_ids?: number[] };
  output_names: Record<string, string>;
  params: Record<string, unknown>;
  idempotency_key?: string;
};

test('cluster commit posts the typed cluster.values action', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-v1-cluster-commit'));
  // "Jon Smith" / "Smith, Jon" collide under the default (token-set)
  // fingerprint -- punctuation stripped, tokens sorted. This is the
  // Python-side single source of truth at
  // tests/cluster_fingerprint_known_pairs.py's JON_SMITH_COLLISION_PAIR
  // (also consumed directly by tests/engine/test_cluster_semantic.py), cross-
  // checked against the real fingerprint() by
  // tests/engine/test_cluster_fingerprint_known_pairs.py. Change the
  // pair there first, then mirror the change here.
  const sheetId = await importCsv(
    page.request,
    pid,
    'names.csv',
    'name\nJon Smith\n"Smith, Jon"\nJon Smith\nJane Doe\n',
  );

  const legacyPosts: string[] = [];
  const v1Posts: PostedAction[] = [];

  for (const legacyRoute of ['cluster', 'resolve']) {
    await page.route(`**/api/projects/${pid}/${legacyRoute}`, async (route) => {
      legacyPosts.push(legacyRoute);
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ error: `${legacyRoute} should use v1 actions` }),
      });
    });
  }
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    v1Posts.push(route.request().postDataJSON() as PostedAction);
    await route.continue();
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await (await revealRibbonAction(page, 'cluster.values')).click();
  await expect(page.getByTestId('action-drawer')).toBeVisible();
  await expect(page.getByTestId('cluster-review-form')).toBeVisible();
  await expect(page.getByTestId('cluster-column-select')).toHaveValue('name');
  await page.getByTestId('cluster-preview-button').click();

  const card = page.getByTestId('cluster-card').first();
  await expect(card).toContainText('Smith, Jon');
  await expect(card).toContainText('Jon Smith');
  // edit the canonical before committing
  await card.getByTestId('cluster-canonical-input').fill('Jonathan Smith');
  await page.getByTestId('cluster-commit-button').click();

  await expect
    .poll(() => v1Posts.find((post) => post.action_id === 'cluster.values'), { timeout: 15_000 })
    .toBeTruthy();
  const clusterPost = v1Posts.find((post) => post.action_id === 'cluster.values')!;
  expect(clusterPost).toMatchObject({
    action_id: 'cluster.values',
    scope: { kind: 'sheet_rows', sheet_id: Number(sheetId) },
    output_names: { canonical: 'name_canonical' },
    params: {
      source: 'name',
      method: 'fingerprint',
      min_size: 2,
    },
  });
  // the reviewed edit + the preview's value_hash ride the commit
  expect(clusterPost.scope).not.toHaveProperty('row_ids');
  const review = clusterPost.params.review as Record<string, unknown>;
  expect(review.source_hash).toEqual(expect.stringMatching(/^sha256:/));
  const overrides = review.canonical_overrides as Record<string, string>;
  expect(Object.values(overrides)).toContain('Jonathan Smith');
  expect(clusterPost.idempotency_key).toEqual(expect.any(String));

  // the cluster UI no longer materializes entities and never hit legacy routes
  expect(v1Posts.some((post) => post.action_id === 'resolve.entities')).toBe(false);
  expect(legacyPosts).toEqual([]);
});
