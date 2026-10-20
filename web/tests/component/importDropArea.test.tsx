// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ImportDropArea } from '../../src/components/importWorkspace/ImportDropArea';

afterEach(cleanup);

describe('shared import drop area', () => {
  it('forwards files, clears the highlight, and stops the outer drop handler', () => {
    const onFiles = vi.fn();
    const outerDrop = vi.fn();
    render(<div onDrop={outerDrop}><ImportDropArea title="Drop files" description="Choose files" testId="drop" onFiles={onFiles}>
      <button>Choose</button>
    </ImportDropArea></div>);
    const area = screen.getByTestId('drop');
    const files = [new File(['hello'], 'test.txt')];
    fireEvent.dragOver(area, { dataTransfer: { files } });
    expect(area).toHaveAttribute('data-dragging', 'true');
    fireEvent.drop(area, { dataTransfer: { files } });
    expect(onFiles).toHaveBeenCalledWith(files);
    expect(outerDrop).not.toHaveBeenCalled();
    expect(area).not.toHaveAttribute('data-dragging');
  });

  it('ignores disabled and empty drops', () => {
    const onFiles = vi.fn();
    const props = { title: 'Drop files', description: '', testId: 'drop', onFiles, children: null };
    const { rerender } = render(<ImportDropArea {...props} disabled />);
    const area = screen.getByTestId('drop');
    fireEvent.drop(area, { dataTransfer: { files: [new File(['hello'], 'test.txt')] } });
    rerender(<ImportDropArea {...props} />);
    fireEvent.drop(area, { dataTransfer: { files: [] } });
    expect(onFiles).not.toHaveBeenCalled();
  });
});
