// @vitest-environment jsdom
//
// Every local-server dead end becomes install guidance aimed at the machine that
// RUNS THE MODEL SERVER — platform guidance is selectable tabs, never sniffed
// from the browser (browser OS ≠ frisket host ≠ model host) — plus a Recheck
// that re-runs the probe. Reachable-but-empty (authoritative listing) gets
// the copyable pull command; unauthorized gets its own state, not "empty".

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  LocalServerGuidance,
  localServerGuidanceState,
} from '../../src/components/LocalServerGuidance';
import type { LocalHttpEndpointEntry } from '../../src/api/types';



afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function entry(overrides: Partial<LocalHttpEndpointEntry>): LocalHttpEndpointEntry {
  return {
    endpoint_id: 'desktop',
    label: 'Local server',
    kind: 'local_http',
    read_only: false,
    models: [],
    reachable: false,
    origin: 'http://localhost:11434',
    authority: 'instance',
    source: 'stored',
    detail: null,
    protocol: 'unknown',
    auth_status: 'unknown',
    token_configured: false,
    provisioning_token_configured: false,
    edge_auth: false,
    pull_enabled: false,
    ...overrides,
  };
}

describe('localServerGuidanceState', () => {
  it('classifies the four states from catalog facts', () => {
    expect(localServerGuidanceState(entry({ reachable: false }))).toBe('unreachable');
    expect(
      localServerGuidanceState(
        entry({ reachable: true, auth_status: 'unauthorized' }),
      ),
    ).toBe('unauthorized');
    expect(
      localServerGuidanceState(
        entry({
          reachable: true,
          auth_status: 'ok',
          protocol: 'ollama_native',
          installed_models: [],
        }),
      ),
    ).toBe('empty_native');
    expect(
      localServerGuidanceState(
        entry({
          reachable: true,
          auth_status: 'ok',
          protocol: 'openai_compatible',
          installed_models: [],
        }),
      ),
    ).toBe('empty_compat');
  });

  it('returns null when models exist or nothing authoritative is known', () => {
    expect(
      localServerGuidanceState(
        entry({
          reachable: true,
          protocol: 'ollama_native',
          installed_models: ['qwen3:0.6b'],
          models: [{ id: 'ollama/@desktop/qwen3:0.6b', label: 'qwen3:0.6b', price: null }],
        }),
      ),
    ).toBeNull();
    // unknown protocol: no authoritative listing — do not claim "empty"
    expect(
      localServerGuidanceState(entry({ reachable: true, protocol: 'unknown' })),
    ).toBeNull();
  });
});

describe('LocalServerGuidance', () => {
  it('unreachable (full): platform tabs are selectable, never sniffed from the browser', async () => {
    render(
      <LocalServerGuidance
        entry={entry({ reachable: false })}
        variant="full"
        onRecheck={vi.fn()}
      />,
    );

    const guidance = screen.getByTestId('local-server-guidance');
    // Guidance targets the model-server machine, not "your computer".
    expect(guidance.textContent).toMatch(/machine/i);
    // All three platforms offered as tabs — no navigator.platform sniffing.
    const macTab = screen.getByTestId('guidance-tab-macos');
    screen.getByTestId('guidance-tab-linux');
    screen.getByTestId('guidance-tab-windows');

    await userEvent.click(screen.getByTestId('guidance-tab-linux'));
    expect(screen.getByTestId('guidance-command')).toHaveTextContent(/install\.sh/);
    await userEvent.click(macTab);
    expect(screen.getByTestId('guidance-command')).toHaveTextContent(/brew install ollama/);
  });

  it('unreachable (compact): no tabs, brief copy plus Recheck', () => {
    render(
      <LocalServerGuidance
        entry={entry({ reachable: false })}
        variant="compact"
        onRecheck={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('guidance-tab-macos')).not.toBeInTheDocument();
    expect(screen.getByTestId('guidance-recheck')).toBeInTheDocument();
  });

  it('empty native server: copyable pull command with clipboard wiring', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(
      <LocalServerGuidance
        entry={entry({
          reachable: true,
          auth_status: 'ok',
          protocol: 'ollama_native',
          installed_models: [],
        })}
        variant="compact"
        onRecheck={vi.fn()}
      />,
    );

    expect(screen.getByTestId('guidance-command')).toHaveTextContent(/ollama pull/);
    await userEvent.click(screen.getByTestId('guidance-copy-command'));
    expect(writeText).toHaveBeenCalledWith(expect.stringMatching(/^ollama pull /));
  });

  it('empty generic server: no ollama pull command, load-a-model copy instead', () => {
    render(
      <LocalServerGuidance
        entry={entry({
          reachable: true,
          auth_status: 'ok',
          protocol: 'openai_compatible',
          installed_models: [],
        })}
        variant="full"
        onRecheck={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('guidance-command')).not.toBeInTheDocument();
    expect(screen.getByTestId('local-server-guidance').textContent).toMatch(/load/i);
  });

  it('unauthorized: names the auth state, offers Recheck', () => {
    render(
      <LocalServerGuidance
        entry={entry({ reachable: true, auth_status: 'unauthorized' })}
        variant="full"
        onRecheck={vi.fn()}
      />,
    );
    expect(screen.getByTestId('local-server-guidance').textContent).toMatch(
      /authentication/i,
    );
  });

  it('Recheck keeps progress on its own button while the targeted probe runs', async () => {
    let finish!: () => void;
    const onRecheck = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    render(
      <LocalServerGuidance
        entry={entry({ reachable: false })}
        variant="compact"
        onRecheck={onRecheck}
      />,
    );
    const button = screen.getByTestId('guidance-recheck');
    await userEvent.click(button);
    expect(onRecheck).toHaveBeenCalled();
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent('Rechecking…');
    finish();
    await waitFor(() => expect(button).toBeEnabled());
    expect(button).toHaveTextContent('Recheck');
  });
});
