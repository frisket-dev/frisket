// jsdom environment gap-fills for browser APIs this codebase's production
// code calls but jsdom 29 does not implement: the Popover API
// (HTMLElement#showPopover/hidePopover + the `:popover-open` pseudo-class),
// `<dialog>#showModal`, and `window.matchMedia`. These are NOT mocks of
// application behavior -- they backfill real platform semantics so component
// tests exercise the actual production call sites (ModelPicker's
// showPopover()/hidePopover() lifecycle, AccountMenu's useNativePopover,
// ConfirmDeleteRowsModal's showModal(), preferences.ts's matchMedia listener)
// instead of stubbing them out.

/** Tracks which elements currently have an "open" popover, so `.matches(':popover-open')`
 *  reflects real showPopover()/hidePopover() calls instead of always returning false. */
const openPopovers = new WeakSet<Element>();

export function installPopoverPolyfill(): void {
  const proto = HTMLElement.prototype as HTMLElement & {
    showPopover?: () => void;
    hidePopover?: () => void;
  };
  if (typeof proto.showPopover !== 'function') {
    proto.showPopover = function (this: HTMLElement) {
      openPopovers.add(this);
    };
  }
  if (typeof proto.hidePopover !== 'function') {
    proto.hidePopover = function (this: HTMLElement) {
      openPopovers.delete(this);
    };
  }
  const matchesProto = Element.prototype as Element & { __popoverPatched?: boolean };
  if (matchesProto.__popoverPatched) return;
  matchesProto.__popoverPatched = true;
  const originalMatches = Element.prototype.matches;
  Element.prototype.matches = function (this: Element, selector: string) {
    if (selector.trim() === ':popover-open') return openPopovers.has(this);
    return originalMatches.call(this, selector);
  };
}

/** `<dialog>#showModal()`/`#close()` are unimplemented in jsdom; ConfirmDeleteRowsModal
 *  and CascadeConfirmDialog's `<dialog>` rely on `.open` toggling. */
export function installDialogPolyfill(): void {
  const proto = HTMLDialogElement.prototype as HTMLDialogElement & {
    showModal?: () => void;
    close?: () => void;
  };
  if (typeof proto.showModal !== 'function') {
    proto.showModal = function (this: HTMLDialogElement) {
      this.open = true;
    };
  }
  if (typeof proto.close !== 'function') {
    proto.close = function (this: HTMLDialogElement) {
      this.open = false;
    };
  }
}

export interface MatchMediaController {
  /** Flip the emulated OS color scheme and notify every live listener, mirroring
   *  Playwright's `page.emulateMedia({ colorScheme })` used by the original e2e specs. */
  setColorScheme(scheme: 'light' | 'dark'): void;
  uninstall(): void;
}

/** jsdom does not implement `window.matchMedia` at all. This installs a minimal,
 *  controllable `(prefers-color-scheme: dark)` media query so preferences.ts's
 *  live matchMedia listener (`resolveThemePreference`, `ensureSystemThemeListener`)
 *  runs for real against a fake-but-real OS-scheme toggle. */
export function installMatchMedia(initialScheme: 'light' | 'dark' = 'light'): MatchMediaController {
  let scheme = initialScheme;
  const listeners = new Set<() => void>();

  const mql = {
    get matches() {
      return scheme === 'dark';
    },
    media: '(prefers-color-scheme: dark)',
    addEventListener: (_type: 'change', listener: () => void) => {
      listeners.add(listener);
    },
    removeEventListener: (_type: 'change', listener: () => void) => {
      listeners.delete(listener);
    },
    addListener: (listener: () => void) => {
      listeners.add(listener);
    },
    removeListener: (listener: () => void) => {
      listeners.delete(listener);
    },
    dispatchEvent: () => true,
  } as unknown as MediaQueryList;

  window.matchMedia = ((query: string) => {
    if (query !== '(prefers-color-scheme: dark)') {
      throw new Error(`installMatchMedia: unsupported query ${query}`);
    }
    return mql;
  }) as typeof window.matchMedia;

  return {
    setColorScheme(next) {
      scheme = next;
      for (const listener of listeners) listener();
    },
    uninstall() {
      listeners.clear();
      // @ts-expect-error -- test-only teardown of a test-only install.
      delete window.matchMedia;
    },
  };
}
