import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const script = await readFile(new URL('../ui/startup.js', import.meta.url), 'utf8');
function harness() {
  let now = 100_000;
  let receive;
  let tick;
  const calls = [];
  const nodes = new Map();
  let document;
  function node() {
    return { hidden: false, disabled: false, textContent: '', dataset: {}, children: [], attributes: {}, events: {},
      scrollTop: 0, scrollHeight: 110, clientHeight: 110,
      replaceChildren(...children) { this.children = children; },
      append(...children) { this.children.push(...children); },
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, fn) { this.events[name] = fn; },
      focus() { document.activeElement = this; },
      getBoundingClientRect() { return { height: 300 }; },
    };
  }
  document = { body: node(), activeElement: null,
    getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
    createElement: node, querySelector: node,
  };
  const bridge = {
    subscribe(fn) { receive = fn; },
    retry() { calls.push('retry'); }, close() { calls.push('close'); },
    copyDiagnostics() { calls.push('copy'); }, details(open) { calls.push(['details', open]); },
    resize(height) { calls.push(['resize', height]); },
  };
  vm.runInNewContext(script, { window: { frisketStartup: bridge }, document,
    Date: class extends Date { static now() { return now; } },
    setInterval(fn) { tick = fn; },
    ResizeObserver: class { observe() {} },
  });
  const get = document.getElementById.bind(document);
  return { get, calls, document, send: receive,
    advance(ms) { now += ms; tick(); },
    click(id) { get(id).events.click(); },
  };
}
function snapshot(overrides = {}) {
  return { version: '1.2.3', platform: 'darwin', osVersion: '24.2.0', elapsedMs: 12_000,
    status: 'running', firstTime: false, currentStep: 'components', activity: 'Installing components',
    steps: [{ id: 'components', state: 'current', elapsedMs: 3_000 }, { id: 'open', state: 'next', elapsedMs: 0 }],
    logs: [], retryable: false, failure: null, ...overrides };
}

test('only the announced launch steps are rendered and single-step launches hide chips', () => {
  const ui = harness();
  assert.equal(ui.get('status').textContent, 'Checking your installation');
  assert.equal(ui.get('steps').hidden, true);
  ui.send(snapshot());
  assert.deepEqual(ui.get('steps').children.map(n => n.textContent), ['App components', 'Open']);
  assert.equal(ui.get('status').textContent, 'Set up app components');
  assert.equal(ui.get('first-time').hidden, true);
  ui.send(snapshot({ steps: [{ id: 'open', state: 'current', elapsedMs: 0 }], currentStep: 'open' }));
  assert.equal(ui.get('steps').hidden, true);
  assert.equal(ui.get('status').textContent, 'Open Frisket');
});

test('only a measured current download gets bytes; raw process terms and percentages stay out', () => {
  const ui = harness();
  ui.send(snapshot({ activity: 'Downloading Python packages 80%', bytes_done: 18_000_000, bytes_total: 32_000_000 }));
  assert.equal(ui.get('activity').textContent, 'Downloading · 18 of 32 MB');
  ui.send(snapshot({ activity: 'Downloading Python packages 80%' }));
  assert.equal(ui.get('activity').textContent, 'Downloading');
  ui.send(snapshot({ activity: 'Installing components', bytes_done: 18_000_000, bytes_total: 32_000_000 }));
  assert.equal(ui.get('activity').textContent, 'Installing components');
});

test('five-minute slowness changes copy only, with quiet process output confined to Details', () => {
  const ui = harness();
  ui.send(snapshot({ firstTime: true, elapsedMs: 3_723_000,
    steps: [{ id: 'components', state: 'current', elapsedMs: 299_000 }],
    logs: [{ elapsedMs: 3_713_000, message: 'Installing pinned packages', tone: 'normal' }] }));
  ui.advance(1_000);
  assert.equal(ui.get('activity').textContent, 'Installing components · still working');
  assert.equal(ui.get('elapsed').textContent, '1:02:04');
  assert.equal(ui.document.body.dataset.failed, 'false');
  assert.equal(ui.get('failure').hidden, true);
  assert.equal(ui.get('first-time').hidden, false);
  assert.equal(ui.get('details').hidden, true);
  ui.click('details-toggle');
  assert.equal(ui.get('details').hidden, false);
  assert.equal(ui.get('quiet').textContent, 'No new output for 11 s · process still running');
  assert.deepEqual(ui.calls, [['details', true]]);
});

test('failure freezes time, focuses Retry only after stopped, and uses guarded bridge actions', () => {
  const ui = harness();
  ui.send(snapshot({ status: 'failed', failure: { cause: 'network' } }));
  assert.equal(ui.get('retry').disabled, true);
  assert.equal(ui.get('status').textContent, "Can't reach the download server");
  assert.equal(ui.get('remedy').textContent, 'Check that this computer is online, then try again.');
  ui.send(snapshot({ status: 'stopped', failure: { cause: 'network' }, retryable: true }));
  assert.equal(ui.document.activeElement, ui.get('retry'));
  ui.advance(10_000);
  assert.equal(ui.get('elapsed').textContent, 'Stopped after 0:12');
  ui.click('retry'); ui.click('retry'); ui.click('failure-copy'); ui.click('close');
  assert.deepEqual(ui.calls, ['retry', 'copy', 'close']);
  assert.equal(ui.document.body.dataset.failed, 'true');
});

test('disk advice uses only reported measurements and unknown failures retain handoff fallback', () => {
  const ui = harness();
  ui.send(snapshot({ status: 'stopped', failure: { cause: 'disk_space', requiredBytes: 3_000_000_000 }, freeDiskBytes: 1_000_000_000 }));
  assert.equal(ui.get('remedy').textContent, 'Frisket needs about 3 GB free. This disk has 1. Free up some space, then try again.');
  ui.send(snapshot({ status: 'stopped', failure: { cause: 'disk_space' } }));
  assert.equal(ui.get('remedy').textContent, 'Free up some space on this disk, then try again.');
  ui.send(snapshot({ status: 'stopped', failure: { cause: 'unknown' } }));
  assert.equal(ui.get('status').textContent, 'Setup stopped before Frisket could open');
  assert.match(ui.get('remedy').textContent, /We don't know what caused this/);
});

test('Help opens real details after launch and logs preserve a reader scrolled up', () => {
  const ui = harness();
  const box = ui.get('setup-log'); box.scrollHeight = 800; box.scrollTop = 0;
  ui.send(snapshot({ status: 'done', detailsOpen: true, detailsOnly: true,
    logs: Array.from({ length: 210 }, (_, i) => ({ elapsedMs: i * 1000, message: `<raw ${i}>`, tone: 'normal' })) }));
  assert.equal(ui.get('details').hidden, false);
  assert.equal(ui.get('close').attributes['aria-label'], 'Close startup details');
  assert.equal(ui.get('retry').hidden, true);
  assert.equal(ui.get('log-lines').children.length, 200);
  assert.equal(ui.get('log-lines').children[0].children[1].textContent, '<raw 10>');
  assert.equal(box.scrollTop, 0);
  assert.equal(ui.get('version').textContent, 'Frisket 1.2.3 · Darwin 24.2.0');
  assert.equal(ui.get('quiet').hidden, true);
  ui.click('details-copy'); ui.click('details-toggle');
  assert.deepEqual(ui.calls, ['copy', ['details', false]]);
});

test('clock ticks do not repeat unchanged live-region announcements', () => {
  const ui = harness(); ui.send(snapshot());
  let writes = 0;
  for (const id of ['status', 'activity', 'remedy']) {
    const node = ui.get(id); let text = node.textContent;
    Object.defineProperty(node, 'textContent', { get: () => text, set(value) { writes++; text = value; } });
  }
  ui.advance(1_000); ui.advance(1_000);
  assert.equal(writes, 0);
  assert.equal(ui.get('elapsed').textContent, '0:14');
});
