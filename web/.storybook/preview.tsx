import type { Preview } from '@storybook/react-vite';
import { Theme } from '../stories/Theme';
import '@fontsource/ibm-plex-sans/400.css';
import '@fontsource/ibm-plex-sans/500.css';
import '@fontsource/ibm-plex-sans/600.css';
import '@fontsource/ibm-plex-sans/700.css';
import '@fontsource/ibm-plex-mono/400.css';
import '@fontsource/ibm-plex-mono/500.css';
import '@fontsource/ibm-plex-mono/600.css';
import '../src/styles.css';
import '../stories/inventory.css';

const preview: Preview = {
  initialGlobals: { theme: 'light' },
  globalTypes: {
    theme: {
      description: 'Production Frisket theme, including portaled controls',
      toolbar: { icon: 'circlehollow', items: ['light', 'dark'], dynamicTitle: true },
    },
  },
  parameters: { layout: 'fullscreen' },
  decorators: [(Story, context) => (
    <Theme theme={context.globals.theme === 'dark' ? 'dark' : 'light'}>
      <div className="inventory-canvas"><Story /></div>
    </Theme>
  )],
};

export default preview;
