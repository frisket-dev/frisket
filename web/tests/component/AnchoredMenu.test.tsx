// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useRef, useState } from 'react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import { AnchoredMenu } from '../../src/components/MenuPop';
import { useNativePopover } from '../../src/hooks/useNativePopover';
import { installPopoverPolyfill } from '../support/domPolyfills';

beforeAll(installPopoverPolyfill);
afterEach(cleanup);

function Harness({ disabled = false, onChoose = () => {} }: { disabled?: boolean; onChoose?(): void }) {
  const [open, setOpen] = useState(false);
  const anchor = useRef<HTMLButtonElement>(null);
  return <>
    <fieldset disabled={disabled}>
      <button ref={anchor} onClick={() => setOpen(true)}>Open</button>
      {open && <AnchoredMenu anchorRef={anchor} onClose={() => setOpen(false)} dismissOnFocusOutside>
        <button onClick={onChoose}>Choose</button>
      </AnchoredMenu>}
    </fieldset>
    <button>Outside</button>
  </>;
}

it('Escape on either trigger or menu restores the trigger and closes only the menu', () => {
  render(<Harness />);
  const trigger = screen.getByText('Open');
  for (const target of ['Open', 'Choose']) {
    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getByText(target), { key: 'Escape' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  }
});

it('outside keyboard focus closes without stealing focus back', () => {
  render(<Harness />);
  fireEvent.click(screen.getByText('Open'));
  const outside = screen.getByText('Outside');
  outside.focus();
  fireEvent.focusIn(outside);
  expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  expect(outside).toHaveFocus();
});

it('keeps a disabled fieldset gate after a menu has portaled outside it', () => {
  const onChoose = vi.fn();
  const { rerender } = render(<Harness onChoose={onChoose} />);
  fireEvent.click(screen.getByText('Open'));
  rerender(<Harness disabled onChoose={onChoose} />);
  fireEvent.click(screen.getByText('Choose'));
  expect(onChoose).not.toHaveBeenCalled();
  expect(screen.queryByRole('menu')).not.toBeInTheDocument();
});

it('a parent defers Escape while its portaled child is open, even with focus on the trigger', () => {
  const closeParent = vi.fn();
  function Nested() {
    const parent = useRef<HTMLDivElement>(null);
    const child = useRef<HTMLDivElement>(null);
    const trigger = useRef<HTMLButtonElement>(null);
    const [open, setOpen] = useState(false);
    useNativePopover(parent, closeParent, { extraRefs: [child] });
    return <div ref={parent}>
      <button ref={trigger} onClick={() => setOpen(true)}>Child</button>
      {open && <AnchoredMenu anchorRef={trigger} menuRef={child} onClose={() => setOpen(false)}>
        <button>Option</button>
      </AnchoredMenu>}
    </div>;
  }
  render(<Nested />);
  const trigger = screen.getByText('Child');
  fireEvent.click(trigger);
  fireEvent.keyDown(trigger, { key: 'Escape' });
  expect(closeParent).not.toHaveBeenCalled();
  expect(screen.queryByText('Option')).not.toBeInTheDocument();
  fireEvent.keyDown(trigger, { key: 'Escape' });
  expect(closeParent).toHaveBeenCalledOnce();
});
