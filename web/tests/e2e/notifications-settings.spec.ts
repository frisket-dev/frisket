import { expect, test } from '@playwright/test';
import { createProject, importCsv, uniqueName } from './helpers';

test('notification settings manages channels routes digests and delivery health', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-notification-settings'));
  await importCsv(
    page.request,
    pid,
    'settings.csv',
    'title,status,body\n' +
      'Budget hearing,open,The council discussed budget oversight.\n' +
      'Budget audit,open,Auditors found budget variances.\n',
  );
  const watchResponse = await page.request.post(`/api/projects/${pid}/watches`, {
    data: {
      name: 'Budget watch',
      scope: { kind: 'project' },
      query: { kind: 'fts', q: 'budget', limit: 10 },
    },
  });
  expect(watchResponse.ok()).toBeTruthy();
  const watch = await watchResponse.json();

  await page.goto(`/p/${pid}/settings/project/notifications`);
  await expect(page.getByTestId('settings-section-project-notifications')).toBeVisible();
  const settings = page.getByTestId('notification-settings-panel');
  await expect(settings).toBeVisible();
  await expect(page.getByTestId('notification-channel-kind')).not.toContainText('Webhook');

  await page.getByTestId('notification-channel-name').fill('Budget Slack');
  await page.getByTestId('notification-channel-slack-label').fill('#budget');
  await page.getByTestId('notification-channel-secret-ref').fill('env:SLACK_WEBHOOK_URL');
  await page.getByTestId('notification-create-channel').click();
  await expect(page.getByTestId('notification-channel-item').filter({ hasText: 'Budget Slack' })).toContainText('secret saved');
  await expect(settings).not.toContainText('SLACK_WEBHOOK_URL');

  await page.getByTestId('notification-route-name').fill('Budget Slack immediate');
  await page.getByTestId('notification-route-watch-id').fill(String(watch.id));
  await page.getByTestId('notification-route-mode').selectOption('immediate');
  await page.getByTestId('notification-create-route').click();
  const slackRoute = page.getByTestId('notification-route-item').filter({ hasText: 'Budget Slack immediate' });
  await expect(slackRoute).toContainText(`watch_id: ${watch.id}`);
  await expect(slackRoute).toContainText('immediate');

  await page.getByTestId('notification-channel-kind').selectOption('email');
  await page.getByTestId('notification-channel-name').fill('Budget Email');
  await page.getByTestId('notification-channel-email-to').fill('alerts@example.com');
  await page.getByTestId('notification-channel-secret-ref').fill('env:RESEND_API_KEY');
  await page.getByTestId('notification-create-channel').click();
  await expect(page.getByTestId('notification-channel-item').filter({ hasText: 'Budget Email' })).toContainText('secret saved');
  await expect(settings).not.toContainText('RESEND_API_KEY');

  await page.getByTestId('notification-route-channel').selectOption({ label: 'Budget Email · alerts@example.com' });
  await page.getByTestId('notification-route-mode').selectOption('digest');
  await page.getByTestId('notification-route-name').fill('Budget daily email digest');
  await page.getByTestId('notification-route-watch-id').fill(String(watch.id));
  await page.getByTestId('notification-route-digest-cadence').selectOption('daily');
  await page.getByTestId('notification-route-digest-anchor').fill('09:00');
  await page.getByTestId('notification-create-route').click();
  const digestRoute = page.getByTestId('notification-route-item').filter({ hasText: 'Budget daily email digest' });
  await expect(digestRoute.getByTestId('notification-digest-preview')).toContainText('daily digest at 09:00 UTC');

  await digestRoute.getByTestId('notification-route-test').click();
  await expect(page.getByTestId('notification-delivery-request').first()).toContainText('test request');
  await expect(page.getByTestId('notification-delivery-request').first()).toContainText('queued');

  await digestRoute.getByTestId('notification-route-toggle').click();
  await expect(digestRoute).toContainText('disabled');
});
