import { stripVTControlCharacters } from 'node:util';
import { homedir, release } from 'node:os';

// Only setup output is recorded, never request bodies or the backend's normal logs.
export function redactSetup(value, home = homedir()) {
  let text = stripVTControlCharacters(String(value));
  if (home) {
    const escaped = home.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    text = text.replace(new RegExp(`${escaped}(?=[\\\\/\\s"']|$)`, 'gi'), '~');
  }
  return text
    .replace(/(https?:\/\/)[^\s/]+@/gi, '$1[redacted]@')
    .replace(/https?:\/\/[^\s"'<>]+/gi, url => url.split(/[?#]/, 1)[0])
    .replace(/((?:access[-_])?(?:token|secret|password|authorization|api[_-]?key)["']?\s*[:=]\s*)[^\r\n]*/gi, '$1[redacted]')
    .replace(/\b([A-Z][A-Z0-9_]{2,}\s*=\s*)[^\r\n]*/g, '$1[redacted]')
    .replace(/\bBearer\s+[^\s]+/gi, 'Bearer [redacted]')
    .replace(/\b[A-Za-z]:[\\/][^\r\n"'<>]+|\\\\[^\s"'<>]+/g, '[path]')
    .replace(/https?:\/\/[^\s"'<>]+|(?<![~\w])\/[^\s"'<>]+/g, match => match.startsWith('http') ? match : '[path]');
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

export function classifySetupFailure(error) {
  const seen = new Set();
  for (let cause = error; cause && !seen.has(cause); cause = cause.cause) {
    seen.add(cause);
    const evidence = `${cause.code || ''} ${cause.message || cause}`;
    if (/\bENOSPC\b|no space left on device|disk (?:is )?full/i.test(evidence)) return 'disk_space';
    if (/\b(?:ENOTFOUND|EAI_AGAIN|ECONNREFUSED|ECONNRESET|ENETUNREACH|EHOSTUNREACH|ETIMEDOUT)\b|could not resolve host|network is unreachable|connection (?:refused|timed out)|temporary failure in name resolution/i.test(evidence)) return 'network';
  }
  return 'unknown';
}

const stepIds = new Set(['engine', 'components', 'open']);
const measured = value => Number.isFinite(value) && value >= 0;

/** One event authority for the startup renderer and the support report. */
export function startupDiagnostics({ version, platform, arch, osVersion = release(), home = homedir(), now = Date.now }) {
  let started = now();
  let ended = null;
  let attempt = 0;
  let status = 'checking';
  let firstTime = false;
  let steps = [];
  let currentStep = null;
  let activity = 'This takes a moment.';
  let failure = null;
  let retryable = false;
  let freeDiskBytes = null;
  let download = {};
  const logs = [];
  const clean = value => redactSetup(value, home).slice(0, 2000);
  const elapsed = () => Math.max(0, (ended ?? now()) - started);
  function record(message, tone = 'normal') {
    let last = '';
    for (const line of String(message).split(/[\r\n]+/)) {
      if (!line.trim()) continue;
      const item = { elapsedMs: elapsed(), message: clean(line), tone: ['normal', 'warning', 'error', 'quiet'].includes(tone) ? tone : 'normal' };
      logs.push(item);
      if (logs.length > 200) logs.shift();
      last = `[${(item.elapsedMs / 1000).toFixed(1)}s] ${item.message}`;
    }
    return last;
  }
  function snapshot() {
    const at = ended ?? now();
    return {
      version: clean(version), platform: clean(platform), arch: clean(arch), osVersion: clean(osVersion),
      attempt, elapsedMs: elapsed(), status, firstTime, currentStep, activity,
      steps: steps.map(step => ({ ...step, elapsedMs: step.startedAt === null ? 0 : Math.max(0, (step.endedAt ?? at) - step.startedAt) })),
      failure: failure ? { ...failure } : null, retryable, freeDiskBytes, ...download,
      logs: logs.map(line => ({ ...line })),
    };
  }
  return {
    record,
    begin() {
      started = now(); ended = null; status = 'checking'; firstTime = false;
      steps = []; currentStep = null; activity = 'This takes a moment.';
      failure = null; retryable = false; freeDiskBytes = null; download = {};
      record(`Startup attempt ${++attempt}`);
    },
    event(event) {
      if (measured(event.freeDiskBytes)) freeDiskBytes = event.freeDiskBytes;
      if (event.type === 'plan') {
        if (steps.length) return; // A launch plan is immutable until Retry begins.
        const ids = event.steps;
        if (!Array.isArray(ids) || !ids.length || ids.length > 3 || new Set(ids).size !== ids.length || ids.some(id => !stepIds.has(id))) return;
        steps = ids.map(id => ({ id, state: 'next', startedAt: null, endedAt: null }));
        firstTime = event.firstTime === true;
        status = 'running';
        record(`Plan: ${ids.join(' → ')}`);
      } else if (event.type === 'step-start') {
        const step = steps.find(item => item.id === event.step);
        if (!step || step.state !== 'next') return;
        currentStep = step.id; step.state = 'current'; step.startedAt = now();
        download = {}; status = 'running';
        record(`Started: ${step.id}`);
      } else if (event.type === 'step-end') {
        const step = steps.find(item => item.id === event.step);
        if (!step || !['next', 'current'].includes(step.state)) return;
        step.state = event.result === 'failed' ? 'stopped' : 'done';
        step.endedAt = now(); download = {};
        record(`Finished: ${step.id} (${['ok', 'failed', 'skipped'].includes(event.result) ? event.result : 'ok'})`);
        if (steps.every(item => item.state === 'done')) { status = 'done'; ended = now(); currentStep = null; }
      } else if (event.type === 'activity') {
        activity = clean(event.message || '');
        download = measured(event.bytes_done) && measured(event.bytes_total) && event.bytes_total > 0 && event.bytes_done <= event.bytes_total
          ? { bytes_done: event.bytes_done, bytes_total: event.bytes_total } : {};
      } else if (event.type === 'failure') {
        const cause = ['network', 'disk_space', 'unknown'].includes(event.cause) ? event.cause : classifySetupFailure(event.error);
        failure = { cause };
        if (measured(event.requiredBytes)) failure.requiredBytes = event.requiredBytes;
        status = 'failed'; retryable = false; download = {};
        const step = steps.find(item => item.id === currentStep);
        if (step) { step.state = 'stopped'; step.endedAt = now(); }
        record(event.error?.message || event.message || `Startup failed: ${cause}`, 'error');
      } else if (event.type === 'stopped') {
        status = 'stopped'; ended = now(); retryable = event.retryable === true;
        record('Startup attempt stopped', 'quiet');
      }
    },
    snapshot,
    report() {
      const state = snapshot();
      const plan = state.steps.map(step => `${step.id}: ${step.state} (${(step.elapsedMs / 1000).toFixed(1)}s)`).join('\n') || 'Checking installation';
      const disk = freeDiskBytes === null ? 'not measured' : `${freeDiskBytes} bytes`;
      const lines = state.logs.map(line => `[${(line.elapsedMs / 1000).toFixed(1)}s] ${line.message}`);
      return `Frisket Desktop ${state.version} (${state.platform}/${state.arch})\nOS: ${state.platform} ${state.osVersion}\nElapsed: ${Math.floor(state.elapsedMs / 1000)}s\nPlan:\n${plan}\nFree disk space: ${disk}\n${lines.join('\n')}`;
    },
  };
}
