// Framework-free store contracts. This file MUST NOT import react/react-dom
// (enforced by the ESLint boundary block in web/eslint.config.js and by
// web/scripts/check-substrate-boundaries.mjs).

export type Listener = () => void;
export type Unsubscribe = () => void;

export interface Store<S> {
  /** Current snapshot. Stable reference until an action produces a new one. */
  get(): S;
  /** Replace state. next may be a value or a (prev)=>next updater. A result
   *  reference-equal to the current snapshot is a no-op (no listener fires). */
  set(next: S | ((prev: S) => S)): void;
  /** Subscribe; returns an unsubscribe. Never called during React render. */
  subscribe(listener: Listener): Unsubscribe;
}

export type Selector<S, T> = (state: S) => T;
export type IsEqual<T> = (a: T, b: T) => boolean;
