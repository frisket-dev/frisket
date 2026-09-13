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
