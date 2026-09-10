// vitest.setup.ts
//
// core/ and state/ are framework-free and run under vitest's `node`
// environment with no jsdom or browser globals. Several state/ stores
// read/write `localStorage` for persisted prefs (gridViewStore's row-
// height/wrap-text, matching workspace/workspaceState.ts's existing
// precedent) — Node has no global `localStorage`, so tests need a minimal
// in-memory stand-in. This is NOT a jsdom shim; it implements only the
// Storage methods this codebase actually calls.

class MemoryStorage implements Storage {
  private store = new Map<string, string>();

  get length(): number {
    return this.store.size;
  }
  clear(): void {
    this.store.clear();
  }
  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key)! : null;
  }
  key(index: number): string | null {
    return [...this.store.keys()][index] ?? null;
  }
  removeItem(key: string): void {
    this.store.delete(key);
  }
  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }
}

globalThis.localStorage = new MemoryStorage();
