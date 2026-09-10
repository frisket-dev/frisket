// The HTML Popover API's `popover` attribute, which @types/react ^18.3 does
// not declare. It is a real, shipped attribute — App.tsx uses it so the error
// toast can join the browser's TOP LAYER, because the import workspace opens
// via dialog.showModal() and no z-index can beat that. Without the toast being
// a popover, errors render invisibly behind the modal.
//
// This is an ambient declaration rather than a cast at the call site: a cast
// would silence the checker for every prop on that element, including a
// genuine typo. Here the widening is exactly one attribute, and the call site
// still feature-detects (`typeof el.showPopover !== 'function'`) so a browser
// without the API degrades instead of throwing.
//
// Delete this file when @types/react ships the attribute — the build fails
// loudly on a duplicate declaration rather than leaving it to rot.
import 'react';

declare module 'react' {
  // `T` is unused HERE and cannot be dropped: declaration merging requires the
  // type parameter list to match React's own `HTMLAttributes<T>` exactly, and
  // renaming it to `_T` would merge into a different interface. The rule has
  // no "type parameters of a merged declaration" exemption, so the exemption
  // is stated here.
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface HTMLAttributes<T> {
    /** "auto" | "manual" — see MDN, Popover API. */
    popover?: 'auto' | 'manual' | '';
  }
}
