// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { createRef } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TextArea, TextInput } from '../../src/components/FormControls';
import { ConfirmTypeInput, SegmentedToggle } from '../../src/components/PanelPrimitives';

afterEach(cleanup);

describe('shared native form controls', () => {
  it('preserves native input attributes, events and refs', () => {
    const ref = createRef<HTMLInputElement>();
    const change = vi.fn();
    render(<TextInput ref={ref} aria-label="Limit" type="number" min={0} max={100} required defaultValue={2} onChange={change} density="compact" fullWidth={false} />);
    const input = screen.getByRole('spinbutton');
    expect(ref.current).toBe(input);
    expect(input).toHaveAttribute('min', '0');
    expect(input).toHaveAttribute('max', '100');
    expect(input).toBeRequired();
    expect(input).toHaveAttribute('data-control-density', 'compact');
    expect(input).toHaveAttribute('data-control-full-width', 'false');
    fireEvent.change(input, { target: { value: '8' } });
    expect(change).toHaveBeenCalledOnce();
    expect(input).toHaveValue(8);
  });

  it('preserves textarea sizing and disabled state', () => {
    const ref = createRef<HTMLTextAreaElement>();
    render(<TextArea ref={ref} aria-label="Description" rows={5} disabled defaultValue={'Two\nlines'} className="form-input custom-layout" />);
    const input = screen.getByRole('textbox');
    expect(input).toBeDisabled();
    expect(input).toHaveAttribute('rows', '5');
    expect(input).toHaveValue('Two\nlines');
    expect(ref.current).toBe(input);
    expect(input.className.split(' ').filter(name => name === 'form-input')).toHaveLength(1);
    expect(input).toHaveClass('form-textarea', 'custom-layout');
  });

  it('keeps confirmation input behavior while using the shared appearance', () => {
    const change = vi.fn();
    render(<ConfirmTypeInput aria-label="Confirm" value="" onChange={change} />);
    const input = screen.getByRole('textbox');
    expect(input).toHaveClass('form-input');
    expect(input).toHaveAttribute('autocomplete', 'off');
    fireEvent.change(input, { target: { value: 'delete' } });
    expect(change).toHaveBeenCalledWith('delete');
  });

  it('sizes segments explicitly without changing selection or disabled behavior', () => {
    const change = vi.fn();
    render(<SegmentedToggle ariaLabel="Preview" size="compact" fullWidth={false} value="native" onValueChange={change} options={[
      { value: 'native', label: 'Text layer' },
      { value: 'ocr', label: 'OCR preview' },
      { value: 'locked', label: 'Unavailable', disabledReason: 'Still loading' },
    ]} />);
    const group = screen.getByRole('group');
    expect(group).toHaveAttribute('data-segment-size', 'compact');
    expect(group).not.toHaveClass('segmented-full');
    expect(screen.getByRole('button', { name: 'Text layer' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: 'Unavailable' }));
    expect(change).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'OCR preview' }));
    expect(change).toHaveBeenCalledWith('ocr');
  });
});
