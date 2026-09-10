// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { SavedView } from '../../src/api/open';
import type { SavedViewsConfirmation } from '../../src/state/savedViewsStore';
import { ViewsPanel } from '../../src/workspace/ViewsPanel';

const view: SavedView = {
  id: 17,
  name: 'Open rows',
  sheet_id: 1,
  spec: { filter: { status: { eq: 'open' } } },
  op_id: null,
};

const originalShowModal = Object.getOwnPropertyDescriptor(
  HTMLDialogElement.prototype,
  'showModal',
);

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value() {},
  });
});

afterEach(() => {
  cleanup();
  if (originalShowModal === undefined) {
    delete (HTMLDialogElement.prototype as Partial<HTMLDialogElement>).showModal;
  } else {
    Object.defineProperty(HTMLDialogElement.prototype, 'showModal', originalShowModal);
  }
});

function FailureHarness() {
  const [confirmation, setConfirmation] = useState<SavedViewsConfirmation | null>(null);
  const [confirmationError, setConfirmationError] = useState<string | null>(null);
  return (
    <ViewsPanel
      views={[view]}
      viewName=""
      editor={null}
      canEdit
      confirmation={confirmation}
      confirmationError={confirmationError}
      onNameChange={() => {}}
      onStartCreating={() => {}}
      onSave={() => {}}
      onUpdate={() => {}}
      onCancel={() => {}}
      onEdit={() => {}}
      onApply={() => {}}
      onStartDefinitionUpdate={(selected) => {
        setConfirmationError(null);
        setConfirmation({ kind: 'update-definition', view: selected });
      }}
      onStartDelete={(selected) => {
        setConfirmationError(null);
        setConfirmation({ kind: 'delete', view: selected });
      }}
      onConfirmDefinitionUpdate={() => setConfirmationError('Update failed')}
      onConfirmDelete={() => setConfirmationError('Delete failed')}
      onCancelConfirmation={() => setConfirmation(null)}
    />
  );
}

describe('ViewsPanel confirmation focus', () => {
  it('returns focus to Delete after a failed request followed by Cancel', async () => {
    render(<FailureHarness />);
    const trigger = screen.getByTestId('delete-saved-view');

    fireEvent.click(trigger);
    fireEvent.click(screen.getByTestId('confirm-delete-saved-view'));
    expect(await screen.findByText('Delete failed')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Cancel'));

    await waitFor(() => expect(trigger).toHaveFocus());
  });
});
