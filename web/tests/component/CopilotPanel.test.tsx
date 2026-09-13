// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { CopilotPanel } from '../../src/components/CopilotPanel';
import { createProjectApi } from '../../src/api/real';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';

vi.mock('../../src/engine-selector/SelectorField', () => ({
  SelectorField: ({ onCurrentChoiceChange }: {
    onCurrentChoiceChange?(choice: {
      authored_selection: { kind: 'model'; model: string };
      can_run: boolean;
    }): void;
  }) => (
    <button
      type="button"
      data-testid="authoritative-copilot-default"
      onClick={() => onCurrentChoiceChange?.({
        authored_selection: { kind: 'model', model: 'server-default' },
        can_run: true,
      })}
    >
      Receive default
    </button>
  ),
}));

let stores: WorkspaceStores | null = null;

afterEach(() => {
  cleanup();
  stores?.dispose();
  stores = null;
  localStorage.clear();
  vi.restoreAllMocks();
});

describe('CopilotPanel selector readiness', () => {
  it('uses a fresh authoritative default without persisting it as a user selection', async () => {
    stores = createWorkspaceStores('project-one', createProjectApi('project-one'));
    const chat = vi.spyOn(stores.projectApi, 'copilotChat').mockResolvedValue({
      reply: 'Ready.',
      proposals: [],
      needsImport: false,
    });
    render(
      <WorkspaceStoresContext.Provider value={stores}>
        <CopilotPanel onRunProposal={vi.fn()} onInspectProposal={vi.fn()} />
      </WorkspaceStoresContext.Provider>,
    );

    fireEvent.change(screen.getByTestId('copilot-input'), { target: { value: 'Summarize this.' } });
    expect(screen.getByTestId('copilot-send')).toBeDisabled();

    fireEvent.click(screen.getByTestId('authoritative-copilot-default'));
    await waitFor(() => expect(screen.getByTestId('copilot-send')).toBeEnabled());
    fireEvent.click(screen.getByTestId('copilot-send'));

    await waitFor(() => expect(chat).toHaveBeenCalledWith(
      [{ role: 'user', content: 'Summarize this.' }],
      'server-default',
    ));
    expect(localStorage.getItem('frisket:copilot-model')).toBeNull();
  });
});
