// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, expect, it, vi, type Mock } from 'vitest';

import { ModelPicker } from '../../src/components/ModelPicker';
import { installPopoverPolyfill } from '../support/domPolyfills';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return { ...actual, listProviders: vi.fn() };
});

import { listProviders } from '../../src/api/open';

beforeAll(() => {
  installPopoverPolyfill();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it('keeps fixed execution leaves in the model list and disables unavailable ones with a reason', async () => {
  (listProviders as unknown as Mock).mockResolvedValue({
    schemaVersion: 'frisket.providers.v1',
    tier: 'local',
    providers: [],
  });
  const onChange = vi.fn();
  render(
    <>
      <span id="runner-label">Run with</span>
      <ModelPicker
        value="local_semantic"
        onChange={onChange}
        ariaLabelledBy="runner-label"
        searchPlaceholder="Search engines and models…"
        emptySearchLabel="No engines or models match"
        choiceGroups={[{
          id: 'local',
          label: 'Local',
          choices: [
            {
              id: 'local_semantic',
              label: 'Semantic match',
              note: 'No provider/API charge · Models: BAAI/bge-small-en-v1.5',
            },
            {
              id: 'missing_local',
              label: 'Missing local engine',
              available: false,
              unavailableReason: 'Install the optional runtime.',
            },
          ],
        }]}
      />
    </>,
  );

  await userEvent.click(screen.getByTestId('model-picker-button'));
  expect(screen.getByTestId('model-provider-group-choice-local')).toHaveTextContent('Local');
  const unavailable = screen.getByTestId('model-option-missing-local');
  expect(unavailable).toBeDisabled();
  expect(unavailable).toHaveTextContent('Install the optional runtime.');

  const search = screen.getByTestId('model-picker-search');
  await userEvent.type(search, 'semantic');
  await userEvent.click(screen.getByTestId('model-option-local-semantic'));
  expect(onChange).toHaveBeenCalledWith('local_semantic');
});
