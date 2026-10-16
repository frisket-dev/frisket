/** Complete optional first-launch prompts through their normal controls. */
export async function handleFirstLaunchPrompts(page) {
  const cloud = page.getByRole('dialog', { name: 'Try AI with Frisket Cloud' });
  await page.addLocatorHandler(cloud, async () => {
    await cloud.getByRole('button', { name: 'Continue without signing in' }).click();
  });
  const telemetry = page.getByTestId('product-telemetry-disclosure');
  await page.addLocatorHandler(telemetry, async () => {
    await telemetry.getByRole('checkbox').uncheck();
    await telemetry.getByRole('button', { name: 'Continue' }).click();
  });
}
