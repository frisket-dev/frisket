// @vitest-environment jsdom
//
// Settings → Preferences → AI call mode, from the 2026-07-26 hand-use pass.
// The section hardcoded "edit /srv/frisket/.env and restart the server with
// docker compose restart" — somebody else's install, for a reader who had
// launched `frisket <dir>` — and rendered its four modes as bordered cards
// with a "Current" badge and no handler, i.e. a chooser that chose nothing.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import * as apiModule from '../../src/api/open';
import type { RuntimeConfig } from '../../src/api/types';
import { AiCallModeSettings } from '../../src/settings/SettingsSections';



function stubConfig(patch: Partial<RuntimeConfig>) {
  vi.spyOn(apiModule, 'getRuntimeConfig').mockResolvedValue({
    cache_mode: 'replay',
    live_calls_possible: true,
    cache_mode_editable: false,
    email_from_address: null,
    email_from_name: null,
    ...patch,
  });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('AI call mode', () => {
  it('describes the env-var mechanism for a direct launch, not compose', async () => {
    stubConfig({ in_container: false });
    render(<AiCallModeSettings />);

    const hint = await screen.findByTestId('ai-call-mode-change-hint');
    expect(hint).toHaveAttribute('data-install-mode', 'process');
    expect(hint).toHaveTextContent('FRISKET_CACHE_MODE=fresh frisket');
    expect(hint).not.toHaveTextContent('docker compose restart');
    expect(hint).not.toHaveTextContent('/srv/frisket');
  });

  it('describes the compose mechanism inside a container', async () => {
    stubConfig({ in_container: true, cache_mode: 'replay_strict' });
    render(<AiCallModeSettings />);

    const hint = await screen.findByTestId('ai-call-mode-change-hint');
    expect(hint).toHaveAttribute('data-install-mode', 'container');
    expect(hint).toHaveTextContent('docker compose restart');
    // Never a made-up path — the old copy's /srv/frisket/.env was an invention.
    expect(hint).not.toHaveTextContent('/srv/frisket');
  });

  it('stays generic when the server does not report its install mode', async () => {
    stubConfig({});
    render(<AiCallModeSettings />);

    const hint = await screen.findByTestId('ai-call-mode-change-hint');
    expect(hint).toHaveAttribute('data-install-mode', 'unknown');
    expect(hint).toHaveTextContent(/set it in the server.s environment/);
  });

  it('renders the modes as an explainer with no interactive control', async () => {
    stubConfig({ in_container: false, cache_mode: 'fresh' });
    const { container } = render(<AiCallModeSettings />);

    await waitFor(() =>
      expect(screen.getByTestId('ai-call-mode-fresh')).toHaveClass('is-current'),
    );
    // Nothing in this section is clickable: it explains a server environment
    // variable, so a control here would be a lie.
    expect(container.querySelectorAll('button, input, select, a, [role="button"]')).toHaveLength(0);
    expect(screen.getByTestId('ai-call-mode-list').tagName).toBe('DL');
  });

  it('saves a local editable mode and marks the returned server value current', async () => {
    stubConfig({ cache_mode_editable: true, cache_mode: 'replay' });
    const update = vi.spyOn(apiModule, 'updateRuntimeConfig').mockResolvedValue({
      cache_mode: 'replay_strict',
      live_calls_possible: false,
      cache_mode_editable: true,
      email_from_address: null,
      email_from_name: null,
    });
    render(<AiCallModeSettings />);

    const strict = await screen.findByRole('radio', { name: /Strict replay/i });
    fireEvent.click(strict);
    fireEvent.click(screen.getByTestId('ai-call-mode-save'));

    await waitFor(() => expect(update).toHaveBeenCalledWith('replay_strict', false));
    expect(screen.getByTestId('ai-call-mode-replay_strict')).toHaveTextContent(/current/i);
  });

  it('requires typed confirmation before leaving strict replay for a live mode', async () => {
    stubConfig({
      cache_mode_editable: true,
      cache_mode: 'replay_strict',
      live_calls_possible: false,
    });
    const update = vi.spyOn(apiModule, 'updateRuntimeConfig').mockResolvedValue({
      cache_mode: 'off',
      live_calls_possible: true,
      cache_mode_editable: true,
      email_from_address: null,
      email_from_name: null,
    });
    render(<AiCallModeSettings />);

    fireEvent.click(await screen.findByRole('radio', { name: /Live without cache/i }));
    fireEvent.click(screen.getByTestId('ai-call-mode-save'));

    expect(screen.getByTestId('ai-call-mode-confirmation')).toBeVisible();
    expect(update).not.toHaveBeenCalled();
    const confirm = screen.getByTestId('ai-call-mode-confirm');
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByTestId('ai-call-mode-confirm-input'), {
      target: { value: 'confirm' },
    });
    fireEvent.click(confirm);

    await waitFor(() => expect(update).toHaveBeenCalledWith('off', true));
  });
});
