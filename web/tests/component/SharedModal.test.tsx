// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ConfirmDialog, Modal } from '../../src/components/Modal';
import { ConfirmDeleteSheetModal } from '../../src/components/ConfirmDeleteSheetModal';
import { CompareDiscardDialog } from '../../src/workbench/CompareDiscardDialog';
import { FormField, SearchInput, TextInput } from '../../src/components/FormControls';
import { Button, TabButton } from '../../src/components/Button';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('normalized controls preserve behavior', () => {
  it('labels fields and forwards native button submission and disabled behavior', () => {
    const submit = vi.fn();
    render(<form onSubmit={event => { event.preventDefault(); submit(); }}>
      <FormField label="Name"><TextInput defaultValue="Example" /></FormField>
      <Button type="submit">Save</Button><Button type="submit" disabled>Blocked</Button>
    </form>);
    expect(screen.getByLabelText('Name')).toHaveValue('Example');
    fireEvent.click(screen.getByText('Blocked')); expect(submit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Save')); expect(submit).toHaveBeenCalledOnce();
  });
  it('preserves search and tab semantics', () => {
    const change = vi.fn();
    render(<><SearchInput aria-label="Documents" onChange={change} /><TabButton aria-selected>History</TabButton></>);
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'report' } });
    expect(change).toHaveBeenCalledOnce();
    expect(screen.getByRole('tab', { name: 'History' })).toHaveAttribute('aria-selected', 'true');
  });
  it('owns cancel without dismissing its parent and blocks cancel while busy', () => {
    const close = vi.fn(), parent = vi.fn();
    const { rerender } = render(<div onCancel={parent}><Modal title="Confirm" onClose={close} busy>Working</Modal></div>);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('open');
    fireEvent(dialog, new Event('cancel', { bubbles: true, cancelable: true }));
    expect(close).not.toHaveBeenCalled(); expect(parent).not.toHaveBeenCalled();
    rerender(<div onCancel={parent}><Modal title="Confirm" onClose={close}>Ready</Modal></div>);
    fireEvent(dialog, new Event('cancel', { bubbles: true, cancelable: true }));
    expect(close).toHaveBeenCalledOnce(); expect(parent).not.toHaveBeenCalled();
  });
  it('does not submit blocked or busy confirmations', () => {
    const confirm = vi.fn();
    const { rerender } = render(<ConfirmDialog title="Delete?" busy onConfirm={confirm} onCancel={vi.fn()}>Details</ConfirmDialog>);
    fireEvent.submit(screen.getByRole('dialog').querySelector('form')!);
    expect(confirm).not.toHaveBeenCalled();
    rerender(<ConfirmDeleteSheetModal sheetName="Source" dependentSheetNames={['Derived']} busy={false} error={null} onConfirm={confirm} onCancel={vi.fn()} />);
    expect(screen.queryByRole('button', { name: 'Delete sheet' })).toBeNull();
    fireEvent.submit(screen.getByRole('dialog').querySelector('form')!);
    expect(confirm).not.toHaveBeenCalled();
  });
  it('only reports a comparison verdict copied after clipboard success', async () => {
    const writeText = vi.fn().mockRejectedValueOnce(new Error('Denied')).mockResolvedValueOnce(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    render(<CompareDiscardDialog description="Unsaved results." verdict="Verdict" testId="ocr-compare" onCancel={vi.fn()} onDiscard={vi.fn()} />);
    fireEvent.click(screen.getByText('Copy verdict'));
    await waitFor(() => expect(writeText).toHaveBeenCalledOnce());
    expect(screen.queryByText('Copied')).toBeNull();
    fireEvent.click(screen.getByText('Copy verdict'));
    await screen.findByText('Copied');
    expect(writeText).toHaveBeenLastCalledWith('Verdict');
    vi.unstubAllGlobals();
  });
});
