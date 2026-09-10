// @vitest-environment jsdom
//
// The sheet tab strip's "Add sheet" button and the toolbar's
// selection-contextual delete button are
// icon-only, carrying their label on aria-label + title (semantic, not
// visible text); the CONFIRMATION dialog still states the real selected row
// count verbatim ("Delete N rows?").
//
// AddSheetButton/DeleteRowsButton were extracted from src/App.tsx (previously
// inline JSX in the giant tab-strip/toolbar render) into
// src/components/TabStripButtons.tsx -- minimal, exported, presentational
// seams so they're mountable in isolation without pulling in App.tsx's own
// heavy transitive dependency graph (workbench contributions, pdfjs, ...),
// which crashes under jsdom (`DOMMatrix is not defined`) on import alone.
// ConfirmDeleteRowsModal was already a standalone exported component.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { AddSheetButton, DeleteRowsButton } from '../../src/components/TabStripButtons';
import { ConfirmDeleteRowsModal } from '../../src/components/ConfirmDeleteRowsModal';
import { installDialogPolyfill } from '../support/domPolyfills';



beforeAll(() => {
  installDialogPolyfill();
});

afterEach(cleanup);

describe('AddSheetButton', () => {
  it('is icon-only with aria-label + title carrying the label', async () => {
    const onClick = vi.fn();
    render(<AddSheetButton onClick={onClick} />);

    const button = screen.getByTestId('workbench-mainView-add-sheet');
    expect(button).toHaveAttribute('aria-label', 'Add sheet');
    expect(button).toHaveAttribute('title', 'Add sheet');
    expect(button.textContent?.trim()).toBe('');
    expect(button.querySelector('svg')).toBeInTheDocument();

    await userEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});

describe('DeleteRowsButton', () => {
  it('is absent (not merely disabled) with no selection', () => {
    render(<DeleteRowsButton count={0} onClick={vi.fn()} />);
    expect(screen.queryByTestId('delete-rows-button')).not.toBeInTheDocument();
  });

  it('is icon-only with aria-label + a title carrying the real selection count', async () => {
    const onClick = vi.fn();
    render(<DeleteRowsButton count={3} onClick={onClick} />);

    const button = screen.getByTestId('delete-rows-button');
    expect(button).toHaveAttribute('aria-label', 'Delete selected rows');
    expect(button).toHaveAttribute('title', 'Delete 3 selected row(s)');
    expect(button.textContent?.trim()).toBe('');
    expect(button.querySelector('svg')).toBeInTheDocument();

    await userEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('tracks a different selection size (real N, not a fixture)', () => {
    render(<DeleteRowsButton count={1} onClick={vi.fn()} />);
    expect(screen.getByTestId('delete-rows-button')).toHaveAttribute(
      'title',
      'Delete 1 selected row(s)',
    );
  });
});

describe('ConfirmDeleteRowsModal (the delete-rows confirmation dialog)', () => {
  it('states the selected row count verbatim, pluralized', () => {
    render(<ConfirmDeleteRowsModal count={3} onConfirm={vi.fn()} onCancel={vi.fn()} />);
    const modal = screen.getByTestId('delete-rows-modal');
    expect(modal).toHaveTextContent('Delete 3 rows?');
  });

  it('singularizes for a count of 1', () => {
    render(<ConfirmDeleteRowsModal count={1} onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByTestId('delete-rows-modal')).toHaveTextContent('Delete 1 row?');
  });

  it('Cancel calls onCancel without confirming', async () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<ConfirmDeleteRowsModal count={3} onConfirm={onConfirm} onCancel={onCancel} />);
    await userEvent.click(screen.getByTestId('delete-rows-cancel'));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
