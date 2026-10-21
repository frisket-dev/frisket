import { defineConfig } from '@playwright/test';
import inventory from './playwright.inventory.config';

// Reuse the disposable local stack; these states need neither Team nor Storybook.
export default defineConfig({
  ...inventory,
  testMatch: 'model-loading.spec.ts',
  webServer: Array.isArray(inventory.webServer) ? inventory.webServer.slice(2, 4) : inventory.webServer,
});
