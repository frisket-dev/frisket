// @vitest-environment jsdom
//
// The python snippet editor's trust line has been wrong in BOTH directions
// inside one day:
//
//   1. a green "● sandboxed / no network · no env · no keys" badge, when only
//      the env third was true — a ctypes call reached the network and read
//      ~/.frisket/secrets/master.key;
//   2. then an unconditional amber "▲ not sandboxed … it can reach the network
//      and read and write files outside the project", which stayed up after
//      the seccomp + Landlock fence landed and made that false on Linux, the
//      one platform Frisket deploys on.
//
// So the contract these tests pin is not a fixed string: the copy must MATCH
// the posture the server reported (`RuntimeConfig.recipe_fence_posture`,
// minted from src/frisket/engine/sandbox/fence.py's platform matrix and
// pinned there by tests/engine/test_sandbox_recipe_fence.py), and every
// posture that is not a reported `enforced` must warn. A missing, failed or
// unrecognised answer is `unknown`, which warns — reassurance requires the
// server to have said so.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as apiModule from '../../src/api/open';
import type { RecipeFencePosture } from '../../src/api/types';
import { mockActionApiDefaults } from '../support/renderActionForm';
import { renderPythonForm } from '../support/pythonActionFixture';
import { PYTHON_SNIPPET_TRUST_COPY } from '../../src/actions/model';







afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

beforeEach(() => {
  mockActionApiDefaults();
});

/** The server's /api/config answer. `posture: undefined` is the older-server /
 *  field-absent case. */
function stubPosture(posture: RecipeFencePosture | undefined | string) {
  vi.spyOn(apiModule, 'getRuntimeConfig').mockResolvedValue({
    cache_mode: 'replay',
    live_calls_possible: true,
    cache_mode_editable: false,
    email_from_address: null,
    email_from_name: null,
    recipe_fence_posture: posture as RecipeFencePosture | undefined,
  });
}


/** The badge, once it reflects a settled posture (the first paint is always
 *  the fail-closed `unknown`). */
async function trustLine(posture: RecipeFencePosture) {
  const warning = await waitFor(() => {
    const el = screen.getByTestId('python-editor-trust-warning');
    expect(el).toHaveAttribute('data-posture', posture);
    return el;
  });
  return { badge: warning, status: warning.parentElement as HTMLElement };
}

describe('python snippet trust line — enforced (server reports a kernel fence)', () => {
  it('says what the fence actually does, and still says it runs on the server', async () => {
    stubPosture('enforced');
    const { container } = renderPythonForm();

    const { badge, status } = await trustLine('enforced');
    expect(badge).toHaveTextContent(/confined by the kernel/i);
    expect(status).toHaveTextContent(/runs on this server/i);
    expect(status).toHaveTextContent(/no network/i);
    expect(status).toHaveTextContent(/scratch directory/i);
    // The refusal half of the promise: a kernel that cannot fence does not
    // silently run the recipe anyway.
    expect(status).toHaveTextContent(/refused rather than run unconfined/i);

    // It must not re-acquire the retracted badge, whatever else it says.
    expect(container.textContent ?? '').not.toMatch(/●\s*sandboxed/i);
    // Not styled as a success/ok state either — the tone that was the bug.
    expect(badge.className).not.toMatch(/status-ok/);
    expect(badge.className).not.toMatch(/status-warn/);
  });

  it('the hint under the editor agrees with the badge above it', async () => {
    stubPosture('enforced');
    const { container } = renderPythonForm();

    await trustLine('enforced');
    const text = container.textContent ?? '';
    expect(text).toMatch(/inside a kernel fence/i);
    // The stale warning must be gone from the hint too, not just the badge.
    expect(text).not.toMatch(/can reach the network/i);
    expect(text).not.toMatch(/files outside the project/i);
  });
});

describe('python snippet trust line — partial (network wall, no filesystem confinement)', () => {
  it('warns, and specifically does not claim the filesystem is confined', async () => {
    stubPosture('partial');
    const { container } = renderPythonForm();

    const { badge, status } = await trustLine('partial');
    expect(badge.className).toMatch(/status-warn/);
    expect(status).toHaveTextContent(/runs on this server/i);
    expect(status).toHaveTextContent(/privileges/i);
    // The one thing this posture must never say.
    expect(status).toHaveTextContent(/does NOT confine the filesystem/i);
    expect(status).toHaveTextContent(/read and write any file/i);
    expect(status).toHaveTextContent(/only run code you trust/i);

    const text = container.textContent ?? '';
    expect(text).not.toMatch(/scratch directory/i);
    expect(text).not.toMatch(/kernel fence/i);
  });
});

describe('python snippet trust line — nothing below Python', () => {
  it('keeps the full warning', async () => {
    stubPosture('none');
    renderPythonForm();

    const { badge, status } = await trustLine('none');
    expect(badge).toHaveTextContent(/not confined/i);
    expect(badge.className).toMatch(/status-warn/);
    expect(status).toHaveTextContent(/runs on this server/i);
    expect(status).toHaveTextContent(/privileges/i);
    expect(status).toHaveTextContent(/reach the network/i);
    expect(status).toHaveTextContent(/files outside the project/i);
    expect(status).toHaveTextContent(/only run code you trust/i);
  });
});

describe('python snippet trust line — an unknown posture never reassures', () => {
  // Four ways the answer can be missing. All of them warn; none of them
  // reassures. This is the fail-closed half of the contract, and the reason
  // the copy is not gated on a catalog flag that a stale catalog could drop.
  const missing: [string, () => void][] = [
    ['the field is absent (older server)', () => stubPosture(undefined)],
    ['the value is unrecognised', () => stubPosture('mostly-fine')],
    [
      'the request fails',
      () => vi.spyOn(apiModule, 'getRuntimeConfig').mockRejectedValue(new Error('offline')),
    ],
    ['the answer has not arrived yet', () => vi.spyOn(apiModule, 'getRuntimeConfig')
      .mockReturnValue(new Promise(() => {}))],
  ];

  for (const [label, stub] of missing) {
    it(`warns when ${label}`, async () => {
      stub();
      const { container } = renderPythonForm();

      const { badge, status } = await trustLine('unknown');
      expect(badge).toHaveTextContent(/confinement unknown/i);
      expect(badge.className).toMatch(/status-warn/);
      expect(badge.className).not.toMatch(/status-ok/);
      expect(status).toHaveTextContent(/assume nothing does/i);
      expect(status).toHaveTextContent(/only run code you trust/i);

      const text = container.textContent ?? '';
      // None of the reassuring vocabulary, in any posture-free form.
      expect(text).not.toMatch(/●\s*sandboxed/i);
      expect(text).not.toMatch(/no network/i);
      expect(text).not.toMatch(/no keys/i);
      expect(text).not.toMatch(/kernel fence/i);
      expect(text).not.toMatch(/confined by the kernel/i);
    });
  }
});

describe('the catalog copy makes no claim it cannot know', () => {
  it('never asserts a sandbox in either direction, and says where the code runs', () => {
    const hint = PYTHON_SNIPPET_TRUST_COPY.unknown.hint;
    expect(hint).not.toMatch(/sandbox/i);
    expect(hint).toMatch(/runs on this server/i);
    expect(hint).toMatch(/assume nothing does/i);
  });
});
