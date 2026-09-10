// Error-CLASS remediation seam (src/errors/remediation.ts) — unit contract
// for the typed (code, details) → user-guidance translation. For
// `model_not_installed`, the
// backend classifier (frisket/llm/remediation.py) types a local-server 404
// with structured not-found evidence and attaches {model, pull_command,
// endpoint_origin}; this seam renders the actionable line — no component
// string-matches the raw message.

import { describe, expect, it } from 'vitest';
import { remediateApiError } from '../../src/errors/remediation';

describe('remediateApiError', () => {
  it('model_not_installed: names the model, surfaces the copyable pull command', () => {
    const remedied = remediateApiError({
      message:
        "The model 'qwen3:0.6b' isn't installed on the local server at http://localhost:11434.",
      code: 'model_not_installed',
      details: {
        model: 'qwen3:0.6b',
        pull_command: 'ollama pull qwen3:0.6b',
        endpoint_origin: 'http://localhost:11434',
      },
    });
    expect(remedied.message).toContain('qwen3:0.6b');
    expect(remedied.remediation).toContain('ollama pull qwen3:0.6b');
    // Class, not brand: guidance targets "your local server", with the brand
    // named only inside the command itself.
    expect(remedied.remediation).toContain('local server');
    expect(remedied.showDiagnose).toBe(true);
  });

  it('model_not_installed without a pull command still guides to the server', () => {
    const remedied = remediateApiError({
      message: 'The requested model is not installed on the local server.',
      code: 'model_not_installed',
      details: { endpoint_origin: 'http://localhost:11434' },
    });
    expect(remedied.remediation).toContain('local server');
    expect(remedied.showDiagnose).toBe(true);
  });

  it('unknown codes keep the original message untouched', () => {
    const remedied = remediateApiError({
      message: 'boom',
      code: 'model_error',
    });
    expect(remedied.message).toBe('boom');
    expect(remedied.remediation).toBeUndefined();
  });
});
