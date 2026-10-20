import { fileURLToPath } from 'node:url';
import type { StorybookConfig } from '@storybook/react-vite';

const config: StorybookConfig = {
  stories: ['../stories/**/*.stories.tsx'],
  framework: {
    name: '@storybook/react-vite',
    options: {
      builder: { viteConfigPath: fileURLToPath(new URL('./vite.config.ts', import.meta.url)) },
    },
  },
  addons: [],
  core: { disableTelemetry: true },
};

export default config;
