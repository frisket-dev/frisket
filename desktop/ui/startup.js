const bridge = window.frisketStartup;
const element = (id) => document.getElementById(id);
function setText(id, value) {
  const node = element(id);
  if (node.textContent !== value) node.textContent = value;
}
const stepCopy = {
  engine: { chip: 'Engine', title: 'Set up the engine' },
  components: { chip: 'App components', title: 'Set up app components' },
  open: { chip: 'Open', title: 'Open Frisket' },
};
let state = { status: 'checking', steps: [], elapsedMs: 0, logs: [], activity: 'This takes a moment.' };
let receivedAt = Date.now();
let detailsOpen = false;
let detailsOnly = false;
let retryRequested = false;
let retryWasAvailable = false;
let logSignature = '';

function clock(milliseconds) {
  const seconds = Math.floor(Math.max(0, Number(milliseconds) || 0) / 1000);
  const minutes = Math.floor(seconds / 60);
  return seconds >= 3600
    ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
    : `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
}
function active() { return state.status === 'checking' || state.status === 'running'; }
function delta() { return active() ? Math.max(0, Date.now() - receivedAt) : 0; }
function failed() { return state.status === 'failed' || state.status === 'stopped'; }
function measured(value) { return typeof value === 'number' && Number.isFinite(value) && value >= 0; }
function gigabytes(value) { return (value / 1_000_000_000).toLocaleString('en', { maximumFractionDigits: 1 }); }

function activityCopy() {
  let message = typeof state.activity === 'string' ? state.activity.trim() : '';
  if (!message) message = state.currentStep === 'open' ? 'Checking everything is ready' : 'Installing components';
  // Process output belongs in Details. Only plain-language activity reaches the card.
  if (/\b(?:python|uv|pip|venv|virtual environment|requirements|packages?|ports?|servers?)\b/i.test(message)) {
    message = /download/i.test(message) ? 'Downloading'
      : /database/i.test(message) ? 'Opening databases'
        : /ready|check/i.test(message) ? 'Checking everything is ready' : 'Installing components';
  }
  if (/%|\b\d+(?:\.\d+)?\s*(?:KB|MB|GB|bytes?)\b|\b\d+\s+of\s+\d+\b/i.test(message)) {
    message = /download/i.test(message) ? 'Downloading' : 'Installing components';
  }
  if (/download/i.test(message) && measured(state.bytes_done) && measured(state.bytes_total)
    && state.bytes_total > 0 && state.bytes_done <= state.bytes_total) {
    const number = (value) => (value / 1_000_000).toLocaleString('en', { maximumFractionDigits: 1 });
    message = `Downloading · ${number(state.bytes_done)} of ${number(state.bytes_total)} MB`;
  }
  const current = (state.steps || []).find((step) => step.id === state.currentStep);
  if (state.status === 'running' && current && (current.elapsedMs || 0) + delta() >= 300_000) {
    message += ' · still working';
  }
  return message;
}
function failureCopy() {
  if (state.failure?.cause === 'network') return [
    "Can't reach the download server", 'Check that this computer is online, then try again.',
  ];
  if (state.failure?.cause === 'disk_space') {
    const free = state.failure.freeBytes ?? state.freeDiskBytes;
    return ['Not enough disk space to finish', measured(state.failure.requiredBytes) && measured(free)
      ? `Frisket needs about ${gigabytes(state.failure.requiredBytes)} GB free. This disk has ${gigabytes(free)}. Free up some space, then try again.`
      : 'Free up some space on this disk, then try again.'];
  }
  return ['Setup stopped before Frisket could open', "We don't know what caused this. Retrying usually works. If it happens again, copy the diagnostics and send them to whoever supports Frisket for you."];
}
function renderLogs() {
  const lines = Array.isArray(state.logs) ? state.logs.slice(-200) : [];
  const signature = JSON.stringify(lines);
  const box = element('setup-log');
  const follow = box.scrollHeight - box.scrollTop - box.clientHeight < 12;
  if (signature !== logSignature) {
    logSignature = signature;
    element('log-lines').replaceChildren(...lines.map((line) => {
      const row = document.createElement('div');
      row.className = `log-line ${['warning', 'error', 'quiet'].includes(line.tone) ? line.tone : 'normal'}`;
      const time = document.createElement('span'); time.className = 'log-time'; time.textContent = clock(line.elapsedMs);
      const message = document.createElement('span'); message.textContent = String(line.message || '');
      row.append(time, message); return row;
    }));
    if (follow) box.scrollTop = box.scrollHeight;
  }
  const quietSeconds = Math.floor(((state.elapsedMs || 0) + delta() - (lines.at(-1)?.elapsedMs || 0)) / 1000);
  element('quiet').hidden = state.status !== 'running' || quietSeconds < 10;
  setText('quiet', `No new output for ${quietSeconds} s · process still running`);
  if (follow) box.scrollTop = box.scrollHeight;
}
function render() {
  const steps = (Array.isArray(state.steps) ? state.steps : []).filter((step) => stepCopy[step.id]);
  document.body.dataset.failed = String(failed());
  document.body.dataset.done = String(state.status === 'done');
  document.body.dataset.details = String(detailsOpen);
  document.body.dataset.chips = String(steps.length > 1);
  element('steps').hidden = steps.length <= 1;
  element('steps').replaceChildren(...steps.map((step) => {
    const chip = document.createElement('li');
    chip.dataset.state = ['done', 'current', 'stopped'].includes(step.state) ? step.state : 'next';
    chip.textContent = `${step.state === 'done' ? '✓ ' : step.state === 'stopped' ? '! ' : ''}${stepCopy[step.id].chip}`;
    if (step.state === 'current') chip.setAttribute('aria-current', 'step');
    return chip;
  }));
  setText('status', failed() ? failureCopy()[0]
    : state.status === 'checking' || !steps.length ? 'Checking your installation'
      : stepCopy[state.currentStep]?.title || 'Open Frisket');
  element('activity').hidden = failed();
  setText('activity', state.status === 'checking' ? 'This takes a moment.' : activityCopy());
  element('failure').hidden = !failed();
  setText('remedy', failed() ? failureCopy()[1] : '');
  element('close').setAttribute('aria-label', detailsOnly ? 'Close startup details' : 'Quit Frisket');
  element('retry').hidden = detailsOnly;
  const canRetry = !detailsOnly && failed() && state.retryable === true && typeof bridge?.retry === 'function';
  element('retry').disabled = !canRetry || retryRequested;
  if (canRetry && !retryWasAvailable) element('retry').focus();
  retryWasAvailable = canRetry;
  element('first-time').hidden = state.firstTime !== true;
  element('details').hidden = !detailsOpen;
  setText('details-toggle', detailsOpen ? 'Hide details' : 'Details');
  element('details-toggle').setAttribute('aria-expanded', String(detailsOpen));
  element('failure-copy').hidden = !failed() || detailsOpen;
  setText('elapsed', `${failed() ? 'Stopped after ' : ''}${clock((state.elapsedMs || 0) + delta())}`);
  const platform = { darwin: 'Darwin', win32: 'Windows', linux: 'Linux' }[state.platform] || state.platform || '';
  setText('version', `Frisket ${state.version || ''}${platform ? ` · ${platform} ${state.osVersion || ''}` : ''}`.trim());
  renderLogs();
}
function receive(snapshot) {
  if (!snapshot || typeof snapshot !== 'object') return;
  state = snapshot; receivedAt = Date.now();
  if (typeof snapshot.detailsOpen === 'boolean') detailsOpen = snapshot.detailsOpen;
  if (typeof snapshot.detailsOnly === 'boolean') detailsOnly = snapshot.detailsOnly;
  if (!failed()) retryRequested = false;
  render();
}
element('details-toggle').addEventListener('click', () => {
  detailsOpen = !detailsOpen; render(); bridge?.details?.(detailsOpen);
});
for (const id of ['failure-copy', 'details-copy']) element(id).addEventListener('click', () => {
  if (typeof bridge?.copyDiagnostics === 'function') Promise.resolve(bridge.copyDiagnostics()).catch(() => {});
});
element('close').addEventListener('click', () => bridge?.close?.());
element('retry').addEventListener('click', () => {
  if (element('retry').disabled || typeof bridge?.retry !== 'function') return;
  retryRequested = true; element('retry').disabled = true;
  Promise.resolve(bridge.retry()).catch(() => { retryRequested = false; render(); });
});
if (typeof bridge?.subscribe === 'function') bridge.subscribe(receive);
if (typeof ResizeObserver === 'function') {
  let lastHeight = 0;
  new ResizeObserver(() => {
    const height = Math.ceil(document.querySelector('.startup-card').getBoundingClientRect().height);
    if (height !== lastHeight && typeof bridge?.resize === 'function') { lastHeight = height; bridge.resize(height); }
  }).observe(document.querySelector('.startup-card'));
}
setInterval(render, 1000);
render();
