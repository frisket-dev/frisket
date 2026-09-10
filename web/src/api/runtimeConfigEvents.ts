import type { RuntimeConfig } from './types';

type Listener = (config: RuntimeConfig) => void;

const listeners = new Set<Listener>();

/** Keep mounted instance-level consumers coherent after Preferences saves. */
export function onRuntimeConfigChanged(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function emitRuntimeConfigChanged(config: RuntimeConfig): void {
  for (const listener of [...listeners]) listener(config);
}
