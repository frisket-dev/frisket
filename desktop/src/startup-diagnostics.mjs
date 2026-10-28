import { stripVTControlCharacters } from 'node:util';

// Only setup output is recorded, never request bodies or the backend's normal logs.
export function redactSetup(value) {
  return stripVTControlCharacters(String(value))
    .replace(/(https?:\/\/)[^\s/]+@/gi, '$1[redacted]@')
    .replace(/((?:access[-_])?(?:token|secret|password|authorization|api[_-]?key)["']?\s*[:=]\s*)[^\r\n]*/gi, '$1[redacted]');
}

/** Keep complete lines so split process output cannot expose partial secrets. */
export function setupOutput(onActivity) {
  let pending = '';
  let dropping = false;
  function finish() {
    if (dropping) onActivity('[oversized setup output omitted]');
    else if (pending.trim()) onActivity(redactSetup(pending).slice(0, 1000));
    pending = '';
    dropping = false;
  }
  return {
    write(chunk) {
      for (const part of String(chunk).split(/([\r\n])/)) {
        if (part === '\r' || part === '\n') finish();
        else if (!dropping) {
          pending += part;
          if (pending.length > 8192) { pending = ''; dropping = true; }
        }
      }
    },
    end: finish,
  };
}

export function startupDiagnostics({ version, platform, arch, now = Date.now }) {
  const started = now();
  let attempt = 0;
  const lines = [];
  return {
    record(message) {
      const line = `[${((now() - started) / 1000).toFixed(1)}s] ${redactSetup(message).slice(0, 2000)}`;
      lines.push(line);
      if (lines.length > 80) lines.shift();
      return line;
    },
    begin() { this.record(`Startup attempt ${++attempt}`); },
    report() {
      return `Frisket Desktop ${version} (${platform}/${arch})\nElapsed: ${Math.floor((now() - started) / 1000)}s\n${lines.join('\n')}`;
    },
  };
}
