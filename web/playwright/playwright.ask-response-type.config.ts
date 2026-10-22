import { defineConfig } from '@playwright/test';
import localStack from './playwright.model-loading.config';

export default defineConfig({ ...localStack, testMatch: 'ask-response-type.spec.ts' });
