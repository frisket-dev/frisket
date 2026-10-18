// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import {
  ImportDestinationChoice,
  ImportDestinationOptions,
} from '../../src/components/importWorkspace/ImportDestinationChoice';

describe('ImportDestinationChoice', () => {
  it('reports the selected destination and only reveals its controls', () => {
    const chooseNew = vi.fn();
    const chooseExisting = vi.fn();

    render(
      <ImportDestinationOptions>
        <ImportDestinationChoice
          name="destination"
          selected={false}
          title="New sheet"
          description="Create a sheet"
          onSelect={chooseNew}
        >
          <input aria-label="Sheet name" />
        </ImportDestinationChoice>
        <ImportDestinationChoice
          name="destination"
          selected
          title="Existing sheet"
          description="Add rows"
          onSelect={chooseExisting}
        >
          <select aria-label="Existing sheet"><option>Archive</option></select>
        </ImportDestinationChoice>
      </ImportDestinationOptions>,
    );

    expect(screen.getByRole('group', { name: 'Import into' })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /Existing sheet/ })).toBeChecked();
    expect(screen.getByLabelText('Existing sheet', { selector: 'select' })).toBeVisible();
    expect(screen.queryByLabelText('Sheet name')).toBeNull();

    fireEvent.click(screen.getByRole('radio', { name: /New sheet/ }));
    expect(chooseNew).toHaveBeenCalledOnce();
    expect(chooseExisting).not.toHaveBeenCalled();
  });
});
