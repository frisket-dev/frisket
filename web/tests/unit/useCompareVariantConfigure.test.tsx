// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useCompareVariantConfigure } from '../../src/workbench/useCompareVariantConfigure';
import { useTranscribeVariantConfigure } from '../../src/workbench/useTranscribeVariantConfigure';
import type { CompareColumn, MediaCompareSession } from '../../src/workbench/mediaCompareSession';
import type { TranscribeCompareEngineResult } from '../../src/api/open';

function Harness() {
  const [columns, setColumns] = useState<CompareColumn[]>([
    { id: 'saved', engineId: null, options: { detail: 'more' } },
    { id: 'chosen', engineId: 'one', options: { detail: 'fewer' } },
  ]);
  const nextId = useRef(0);
  const editor = useCompareVariantConfigure({
    columns,
    addBlankColumn: () => {
      const id = `new-${++nextId.current}`;
      setColumns((current) => [...current, { id, engineId: null, options: {} }]);
      return id;
    },
    removeColumn: (id) => setColumns((current) => current.filter((column) => column.id !== id)),
    chooseEngine: (id, engineId) => setColumns((current) => current.map((column) => (
      column.id === id ? { ...column, engineId } : column
    ))),
    duplicateColumn: (id) => {
      const source = columns.find((column) => column.id === id);
      if (!source) return null;
      const copyId = `copy-${++nextId.current}`;
      setColumns((current) => [...current, { ...source, id: copyId, options: { ...source.options } }]);
      return copyId;
    },
  });
  return <>
    <button onClick={editor.addVariant}>Add</button>
    <button>Outside</button>
    {columns.map((column) => <div key={column.id} data-testid={column.id}
      ref={editor.configureVariantId === column.id ? editor.configureAnchorRef : undefined}>
      <button onClick={() => editor.openConfigure(column.id)}>Edit {column.id}</button>
      <button onClick={() => editor.onRemoveVariant(column.id)}>Remove {column.id}</button>
      <output>{JSON.stringify(column)}</output>
      {editor.configureVariantId === column.id && <div data-testid="editor"
        ref={editor.configurePopoverRef}
        style={{ transform: `translateX(${editor.popoverShift}px)` }}>
        <button ref={editor.engineSelectorRef}
          onClick={() => editor.onChooseEngine(column.id, 'two')}>Choose</button>
        <button onClick={() => editor.onDuplicate(column.id)}>Duplicate</button>
        <input aria-label="Option" />
      </div>}
    </div>)}
    {createPortal(<div className="engine-selector__dialog">
      <button>Nested selector</button>
    </div>, document.body)}
  </>;
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('comparison variant editor lifecycle', () => {
  it.each(['escape', 'outside', 'toggle', 'switch', 'another add'] as const)(
    'discards only an abandoned provisional variant on %s', async (action) => {
      const user = userEvent.setup();
      render(<Harness />);
      await user.click(screen.getByRole('button', { name: 'Add' }));
      expect(screen.getByTestId('new-1')).toBeInTheDocument();
      if (action === 'escape') await user.keyboard('{Escape}');
      if (action === 'outside') await user.click(screen.getByRole('button', { name: 'Outside' }));
      if (action === 'toggle') await user.click(screen.getByRole('button', { name: 'Edit new-1' }));
      if (action === 'switch') await user.click(screen.getByRole('button', { name: 'Edit saved' }));
      if (action === 'another add') await user.click(screen.getByRole('button', { name: 'Add' }));
      expect(screen.queryByTestId('new-1')).not.toBeInTheDocument();
      expect(screen.getByTestId('saved')).toHaveTextContent('"detail":"more"');
      if (action === 'another add') expect(screen.getByTestId('new-2')).toBeInTheDocument();
    },
  );

  it('keeps existing blank drafts and newly committed variants on dismissal', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole('button', { name: 'Edit saved' }));
    await user.keyboard('{Escape}');
    expect(screen.getByTestId('saved')).toHaveTextContent('"detail":"more"');
    await user.click(screen.getByRole('button', { name: 'Add' }));
    await user.click(screen.getByRole('button', { name: 'Choose' }));
    await user.keyboard('{Escape}');
    expect(screen.getByTestId('new-1')).toHaveTextContent('"engineId":"two"');
  });

  it('focuses on open, leaves option focus alone, and returns Escape focus to the opener', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const opener = screen.getByRole('button', { name: 'Edit chosen' });
    await user.click(opener);
    expect(screen.getByRole('button', { name: 'Choose' })).toHaveFocus();
    await user.click(screen.getByRole('textbox', { name: 'Option' }));
    expect(screen.getByRole('textbox', { name: 'Option' })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(opener).toHaveFocus();
    expect(screen.queryByTestId('editor')).not.toBeInTheDocument();
  });

  it('ignores portaled selector pointer/Escape but closes on the parent Escape', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole('button', { name: 'Add' }));
    const nested = screen.getByRole('button', { name: 'Nested selector' });
    await user.click(nested);
    await user.keyboard('{Escape}');
    expect(screen.getByTestId('editor')).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('button', { name: 'Choose' }), { key: 'Escape' });
    expect(screen.queryByTestId('new-1')).not.toBeInTheDocument();
  });

  it('opens duplicates with copied settings and clears the editor on remove', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole('button', { name: 'Edit chosen' }));
    await user.click(screen.getByRole('button', { name: 'Duplicate' }));
    expect(screen.getByTestId('copy-1')).toHaveTextContent('"detail":"fewer"');
    expect(screen.getByRole('button', { name: 'Choose' })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.getByTestId('copy-1')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Edit copy-1' }));
    await user.click(screen.getByRole('button', { name: 'Remove copy-1' }));
    expect(screen.queryByTestId('editor')).not.toBeInTheDocument();
    expect(screen.queryByTestId('copy-1')).not.toBeInTheDocument();
  });

  it('discards a blank duplicate without discarding the existing source draft', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole('button', { name: 'Edit saved' }));
    await user.click(screen.getByRole('button', { name: 'Duplicate' }));
    await user.keyboard('{Escape}');
    expect(screen.queryByTestId('copy-1')).not.toBeInTheDocument();
    expect(screen.getByTestId('saved')).toBeInTheDocument();
  });

  it('reclamps on scroll and resize without accumulating previous translations', async () => {
    let naturalLeft = window.innerWidth - 100;
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const shift = Number(this.style.transform.match(/translateX\((-?[\d.]+)px\)/)?.[1] ?? 0);
      return { left: naturalLeft + shift, right: naturalLeft + shift + 300 } as DOMRect;
    });
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole('button', { name: 'Edit chosen' }));
    expect(screen.getByTestId('editor')).toHaveStyle({ transform: 'translateX(-208px)' });
    fireEvent.scroll(window);
    expect(screen.getByTestId('editor')).toHaveStyle({ transform: 'translateX(-208px)' });
    naturalLeft = 100;
    fireEvent.resize(window);
    expect(screen.getByTestId('editor')).toHaveStyle({ transform: 'translateX(0px)' });
    naturalLeft = window.innerWidth - 100;
    fireEvent.scroll(window);
    await user.keyboard('{Escape}');
    naturalLeft = 100;
    await user.click(screen.getByRole('button', { name: 'Edit saved' }));
    expect(screen.getByTestId('editor')).toHaveStyle({ transform: 'translateX(0px)' });
  });

  it('retains Transcribe-specific option resets when committing an engine', () => {
    const chooseEngine = vi.fn();
    const updateColumnOptions = vi.fn();
    const session = { columns: [], chooseEngine, updateColumnOptions,
      addBlankColumn: vi.fn(), removeColumn: vi.fn(), duplicateColumn: vi.fn(),
    } as unknown as MediaCompareSession<TranscribeCompareEngineResult>;
    const { result } = renderHook(() => useTranscribeVariantConfigure(session));
    act(() => result.current.onConfigureChooseEngine('variant', 'whisper'));
    expect(chooseEngine).toHaveBeenCalledWith('variant', 'whisper');
    expect(updateColumnOptions).toHaveBeenCalledWith('variant', {
      language: undefined, modelSize: undefined, vad: undefined, diarize: undefined,
      numSpeakers: undefined, minSpeakers: undefined, maxSpeakers: undefined,
    });
  });
});
