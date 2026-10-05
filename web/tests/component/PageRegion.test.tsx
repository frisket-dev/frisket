// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PageRegion } from '../../src/components/PageRegion';

const box = { x0: .1, y0: .2, x1: .3, y1: .4 };
beforeEach(() => {
  vi.stubGlobal('PointerEvent', class extends MouseEvent {
    readonly pointerId: number;
    constructor(type: string, init: PointerEventInit = {}) { super(type, init); this.pointerId = init.pointerId ?? 1; }
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function frame() {
  const region = screen.getByRole('button');
  vi.spyOn(region.parentElement!, 'getBoundingClientRect').mockReturnValue({ left: 100, top: 50, width: 1000, height: 500 } as DOMRect);
  region.setPointerCapture = vi.fn();
  region.hasPointerCapture = vi.fn(() => true);
  region.releasePointerCapture = vi.fn();
  return region;
}

describe('shared page regions', () => {
  it('renders exact tiny geometry read-only, without a minimum size or controls', () => {
    const { container, rerender } = render(<PageRegion box={{ x0: .1, y0: .2, x1: .102, y1: .204 }} data-testid="region" />);
    const region = screen.getByTestId('region');
    expect(region.tagName).toBe('SPAN');
    expect(parseFloat(region.style.width)).toBeCloseTo(.2);
    expect(parseFloat(region.style.height)).toBeCloseTo(.4);
    expect(screen.queryByRole('button')).toBeNull();
    expect(region.querySelector('[data-corner]')).toBeNull();
    rerender(<PageRegion box={{ ...box, x1: box.x0 }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('drags in page coordinates, commits only on release, and clamps at the edge', () => {
    const onChange = vi.fn(); const parentPointer = vi.fn();
    render(<div onPointerDown={parentPointer}><PageRegion box={box} draggable onChange={onChange} /></div>);
    const region = frame();
    fireEvent.pointerDown(region, { clientX: 250, clientY: 200, pointerId: 1 });
    fireEvent.pointerMove(region, { clientX: 450, clientY: 250, pointerId: 2 });
    expect(parseFloat(region.style.left)).toBeCloseTo(10);
    fireEvent.pointerMove(region, { clientX: 450, clientY: 250, pointerId: 1 });
    expect(parseFloat(region.style.left)).toBeCloseTo(30);
    expect(onChange).not.toHaveBeenCalled();
    expect(parentPointer).not.toHaveBeenCalled();
    fireEvent.pointerUp(region, { clientX: 1500, clientY: 1000, pointerId: 1 });
    const next = onChange.mock.calls[0][0];
    expect(next.x0).toBeCloseTo(.8); expect(next.x1).toBe(1);
    expect(next.y0).toBeCloseTo(.8); expect(next.y1).toBe(1);
    expect(region.releasePointerCapture).toHaveBeenCalledWith(1);
    expect(region.querySelector('[data-corner]')).toBeNull();
  });

  it('supports resizing independently of dragging, including sub-percent boxes', () => {
    const onChange = vi.fn();
    render(<div><PageRegion box={box} selected resizable onChange={onChange} /></div>);
    const region = frame();
    fireEvent.pointerDown(region, { clientX: 250, clientY: 200 });
    fireEvent.pointerUp(region, { clientX: 450, clientY: 250 });
    expect(onChange).not.toHaveBeenCalled();
    const handle = region.querySelector('[data-corner="se"]')!;
    fireEvent.pointerDown(handle, { clientX: 400, clientY: 250 });
    fireEvent.pointerUp(region, { clientX: 201, clientY: 151 });
    expect(onChange).toHaveBeenCalledWith({ x0: .1, y0: .2, x1: .101, y1: .202 });
  });

  it('keeps full-width bands full-width when resizing or moving', () => {
    const onChange = vi.fn();
    render(<div><PageRegion box={{ ...box, x0: 0, x1: 1 }} selected draggable resizable axis="y" onChange={onChange} /></div>);
    const region = frame();
    fireEvent.pointerDown(region.querySelector('[data-corner="nw"]')!, { clientX: 100, clientY: 150 });
    fireEvent.pointerUp(region, { clientX: 500, clientY: 100 });
    expect(onChange).toHaveBeenLastCalledWith({ x0: 0, y0: .1, x1: 1, y1: .4 });
    fireEvent.keyDown(region, { key: 'ArrowRight', shiftKey: true });
    expect(onChange).toHaveBeenLastCalledWith({ ...box, x0: 0, x1: 1 });
  });

  it('cancels an interrupted gesture without committing', () => {
    const onChange = vi.fn();
    render(<div><PageRegion box={box} draggable onChange={onChange} /></div>);
    const region = frame();
    fireEvent.pointerDown(region, { clientX: 250, clientY: 200 });
    fireEvent.pointerMove(region, { clientX: 450, clientY: 250 });
    fireEvent.pointerCancel(region);
    expect(parseFloat(region.style.left)).toBeCloseTo(10);
    fireEvent.pointerUp(region, { clientX: 450, clientY: 250 });
    expect(onChange).not.toHaveBeenCalled();
  });

  it('handles keyboard movement and deletion once, and disables all editing when muted', () => {
    const onChange = vi.fn(); const onDelete = vi.fn(); const onSelect = vi.fn(); const outerKey = vi.fn();
    const { rerender } = render(<div onKeyDown={outerKey}><PageRegion box={box} draggable onChange={onChange} onDelete={onDelete} /></div>);
    fireEvent.keyDown(screen.getByRole('button'), { key: 'ArrowDown', shiftKey: true });
    expect(onChange.mock.calls[0][0].y0).toBeCloseTo(.21);
    fireEvent.keyDown(screen.getByRole('button'), { key: 'Delete' });
    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(outerKey).not.toHaveBeenCalled();
    onChange.mockClear(); onDelete.mockClear();
    rerender(<PageRegion box={box} disabled selected draggable resizable onChange={onChange} onDelete={onDelete} onSelect={onSelect} />);
    const region = screen.getByRole('button');
    expect(region).toBeDisabled();
    fireEvent.keyDown(region, { key: 'ArrowDown' }); fireEvent.keyDown(region, { key: 'Delete' }); fireEvent.click(region);
    expect(onChange).not.toHaveBeenCalled(); expect(onDelete).not.toHaveBeenCalled(); expect(onSelect).not.toHaveBeenCalled();
    expect(region.querySelector('[data-corner]')).toBeNull();
  });
});
