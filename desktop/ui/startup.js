const stages = ['runtime', 'dependencies', 'browser', 'workspace'];
const defaults = {
  runtime: 'Setting up Frisket Desktop…',
  dependencies: 'Putting the finishing touches in place…',
  browser: 'Preparing the tools you use in Frisket…',
  workspace: 'Opening your workspace…',
  ready: 'Frisket Desktop is ready.',
};

const status = document.querySelector('#status');
const progress = document.querySelector('#progress');
const setupProgress = document.querySelector('.setup-progress');
const stageNodes = [...document.querySelectorAll('[data-phase]')];

/** Update the trusted desktop startup screen. */
window.updateStartup = ({ phase = 'runtime', message } = {}) => {
  const isReady = phase === 'ready';
  const current = stages.indexOf(phase);
  if (!isReady && current === -1) return;

  const completed = isReady ? stages.length : current;
  const active = isReady ? -1 : current;
  const label = isReady ? 'Setup complete' : `Step ${current + 1} of ${stages.length}`;

  document.body.dataset.ready = String(isReady);
  status.textContent = message || defaults[phase];
  setupProgress.style.setProperty('--complete', String(completed));
  progress.setAttribute('aria-valuenow', String(completed));
  progress.setAttribute('aria-valuetext', `${label}: ${status.textContent}`);

  stageNodes.forEach((node, index) => {
    node.dataset.state = index < completed ? 'complete' : index === active ? 'active' : 'pending';
  });
};

window.updateStartup();

const activity = document.querySelector('#activity');
const elapsed = document.querySelector('#elapsed');
const log = document.querySelector('#setup-log');
const startedAt = Date.now();
let lastActivityAt = startedAt;
const lines = [];
window.updateStartupActivity = (line) => {
  lastActivityAt = Date.now();
  activity.textContent = line.replace(/^\[[^\]]+\] /, '');
  lines.push(line);
  if (lines.length > 40) lines.shift();
  log.textContent = lines.join('\n');
  log.scrollTop = log.scrollHeight;
};
setInterval(() => {
  if (document.body.dataset.ready === 'true') return;
  const seconds = Math.floor((Date.now() - startedAt) / 1000);
  const quiet = Math.floor((Date.now() - lastActivityAt) / 1000);
  elapsed.textContent = `${Math.floor(seconds / 60)}m ${seconds % 60}s elapsed` + (quiet >= 10 ? ` · No new output for ${quiet}s` : '');
}, 1000);
