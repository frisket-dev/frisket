// The action panel's
// credential gate ("Needs an API key. <action> requires CENSUS_API_KEY to
// run." → "Add in Settings →") lands on the bare secrets page and makes the
// user retype the exact env-var name from memory. The link should carry the
// name, and the page should meet them halfway.
//
// DONE means:
// - /p/{pid}/settings/project/secrets?secret=CENSUS_API_KEY pre-fills the
//   Name field with CENSUS_API_KEY and focuses the Value field.
// - The Value field is VISIBLE while typing (type=text by default) with a
//   show/hide toggle (data-testid secret-value-visibility) that flips it to
//   password-masked. (The stored-secrets table already masks to a last-4
//   hint server-side — security/secrets.py::key_hint — no change needed.)
// - The credential gate's "Add in Settings →" navigates with the missing
//   credential name riding the ?secret= param.

import { expect, test } from '@playwright/test';
import { createProject, uniqueName } from './helpers';

test('secrets deep-link prefills the name and focuses a visible value field', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-secret-link'));
  await page.goto(`/p/${pid}/settings/project/secrets?secret=CENSUS_API_KEY`);

  await expect(page.getByTestId('project-secrets-settings')).toBeVisible();
  const name = page.getByLabel('Project secret name');
  await expect(name).toHaveValue('CENSUS_API_KEY');

  const value = page.getByLabel('Project secret value');
  await expect(value).toBeFocused();
  await expect(value).toHaveAttribute('type', 'text'); // visible while typing

  await value.fill('census-sekrit-1234');
  await page.getByTestId('secret-value-visibility').click();
  await expect(value).toHaveAttribute('type', 'password');
  await page.getByTestId('secret-value-visibility').click();
  await expect(value).toHaveAttribute('type', 'text');
});

test('a plain visit keeps the form empty and unfocused', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-secret-plain'));
  await page.goto(`/p/${pid}/settings/project/secrets`);

  await expect(page.getByTestId('project-secrets-settings')).toBeVisible();
  await expect(page.getByLabel('Project secret name')).toHaveValue('');
  await expect(page.getByLabel('Project secret value')).not.toBeFocused();
});
