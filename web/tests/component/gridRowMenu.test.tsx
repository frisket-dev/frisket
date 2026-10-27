// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { GridRowMenu } from '../../src/workspace/gridMenus';

describe('row context menu', () => {
  it('uses keyboard navigation and returns focus on Escape', () => {
    const grid = document.createElement('button');
    document.body.append(grid);
    grid.focus();
    const onClose = vi.fn();
    const onSplit = vi.fn();
    const view = render(<GridRowMenu bounds={{ x: 100, y: 100, width: 30, height: 20 }}
      rowIndex={2} rowCount={4} canSplit selectionLoaded={false}
      onOpen={vi.fn()} onSplit={onSplit} onOcr={vi.fn()} onCopy={vi.fn()} onDelete={vi.fn()} onClose={onClose} />);
    expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: 'Open in Document view' }));
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
    const split = screen.getByRole('menuitem', { name: 'Split PDFs…' });
    expect(document.activeElement).toBe(split);
    fireEvent.click(split);
    expect(onSplit).toHaveBeenCalledOnce();
    expect(screen.getByRole('menuitem', { name: 'OCR 4 rows' })).toBeDisabled();
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(grid);
    view.unmount();
    grid.remove();
  });
});
