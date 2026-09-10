import type { APIRequestContext, APIResponse } from '@playwright/test';
import { describe, expect, test, vi } from 'vitest';

import { runAndWait } from '../e2e/helpers';

function response(status: number, body: unknown): APIResponse {
  return {
    status: () => status,
    ok: () => status >= 200 && status < 300,
    json: async () => body,
  } as unknown as APIResponse;
}

function harness(postResponses: APIResponse[]) {
  const pending = [...postResponses];
  const post = vi.fn<(
    url: string,
    options: { data: unknown },
  ) => Promise<APIResponse>>(async () => {
    const next = pending.shift();
    if (!next) throw new Error('unexpected POST');
    return next;
  });
  const get = vi.fn<(url: string) => Promise<APIResponse>>(async () => response(200, {
    run: {
      status: 'completed',
      public_status: { status: 'completed', live: false },
    },
  }));
  return {
    request: { post, get } as unknown as APIRequestContext,
    post,
    get,
  };
}

function completed(runId = 7): APIResponse {
  return response(200, {
    schema_version: 'frisket.action_result.v1',
    status: 'queued',
    run_id: runId,
  });
}

describe('runAndWait action contract', () => {
  test('preserves a supplied canonical row scope', async () => {
    const { request, post } = harness([completed()]);

    await runAndWait(request, 'project-1', {
      schema_version: 'frisket.action.v2',
      kind: 'map.ner',
      capabilities: ['project:write'],
      row_scope: {
        sheet_id: 3,
        selector: { kind: 'exact_membership', membership: { row_ids: [2, 4] } },
      },
      params: {
        input_columns: ['snippet'],
        labels: ['person'],
        engine: 'spacy',
        output_name: 'entities',
      },
    });

    expect(post).toHaveBeenCalledTimes(1);
    const action = post.mock.calls[0]?.[1]?.data as Record<string, unknown>;
    expect(action.row_scope).toEqual({
      sheet_id: 3,
      selector: { kind: 'exact_membership', membership: { row_ids: [2, 4] } },
    });
  });

  test('posts a registered row scope without translating a retired recipe alias', async () => {
    const { request, post } = harness([completed()]);

    await runAndWait(request, 'project-1', {
      action_id: 'map.template',
      scope: { kind: 'sheet_rows', sheet_id: 3, row_ids: [4, 2] },
      params: { template: { text: '{{snippet}}' } },
      output_names: { rendered: 'match' },
      idempotency_key: 'e2e-helper-map.template:row-scope',
    });

    const action = post.mock.calls[0]?.[1]?.data as {
      params: Record<string, unknown>;
      scope: Record<string, unknown>;
    };
    expect(action.params).not.toHaveProperty('sheet_id');
    expect(action.params).not.toHaveProperty('row_ids');
    expect(action.scope).toEqual({
      kind: 'sheet_rows',
      sheet_id: 3,
      row_ids: [4, 2],
    });
  });

  test('repairs an older schema-v2 row-scoped helper payload', async () => {
    const { request, post } = harness([completed()]);

    await runAndWait(request, 'project-1', {
      schema_version: 'frisket.action.v2',
      kind: 'map.ner',
      capabilities: ['project:write'],
      params: {
        sheet_id: 3,
        input_columns: ['snippet'],
        labels: ['person'],
        engine: 'spacy',
        output_name: 'entities',
      },
    });

    const action = post.mock.calls[0]?.[1]?.data as {
      params: Record<string, unknown>;
      row_scope: Record<string, unknown>;
    };
    expect(action.params).not.toHaveProperty('sheet_id');
    expect(action.row_scope).toEqual({
      sheet_id: 3,
      selector: { kind: 'all_rows' },
    });
  });

  test('echoes the exact 402 hash with the same action identity', async () => {
    const challenge = response(402, {
      schema_version: 'frisket.action_result.v1',
      status: 'needs_confirmation',
      errors: [{
        message: 'confirm cost',
        details: { promise_set_hash: 'sha256:server-authored' },
      }],
    });
    const { request, post } = harness([challenge, completed(11)]);

    await expect(runAndWait(request, 'project-1', {
      action_id: 'map.summarize',
      scope: { kind: 'sheet_rows', sheet_id: 3 },
      params: {
        source: ['snippet'],
        model: 'gemini/gemini-2.5-flash',
        preset: 'paragraph',
        instruction: null,
        context: '',
      },
      output_names: { summary: 'summary' },
      idempotency_key: 'e2e-helper-map.summarize:registered',
      confirmation: 'sha256:caller-supplied',
    })).resolves.toBe(11);

    expect(post).toHaveBeenCalledTimes(2);
    const first = post.mock.calls[0]?.[1]?.data as {
      action_id: string;
      idempotency_key: string;
      confirmation?: string;
    };
    const retry = post.mock.calls[1]?.[1]?.data as typeof first;
    expect(first.action_id).toBe('map.summarize');
    expect(first.idempotency_key).toBe('e2e-helper-map.summarize:registered');
    expect(first.confirmation).toBe('sha256:caller-supplied');
    expect(retry.action_id).toBe(first.action_id);
    expect(retry.idempotency_key).toBe(first.idempotency_key);
    expect(retry.confirmation).toBe('sha256:server-authored');
  });

  test('fails clearly on a malformed 402 envelope', async () => {
    const { request, post } = harness([response(402, {
      schema_version: 'frisket.action_result.v1',
      status: 'needs_confirmation',
      errors: [{ message: 'confirm cost', details: {} }],
    })]);

    await expect(runAndWait(request, 'project-1', {
      action_id: 'map.summarize',
      scope: { kind: 'sheet_rows', sheet_id: 3 },
      params: {
        source: ['snippet'],
        model: 'gemini/gemini-2.5-flash',
        preset: 'paragraph',
        instruction: null,
        context: '',
      },
      output_names: { summary: 'summary' },
      idempotency_key: 'e2e-malformed-challenge',
    })).rejects.toThrow(
      'malformed HTTP 402 needs_confirmation response: expected nonempty errors[0].details.promise_set_hash',
    );
    expect(post).toHaveBeenCalledTimes(1);
  });

  test('keeps a free action single-post', async () => {
    const { request, post } = harness([completed()]);
    await runAndWait(request, 'project-1', {
      action_id: 'map.template',
      scope: { kind: 'sheet_rows', sheet_id: 3 },
      params: { template: { text: '{{snippet}}' } },
      output_names: { rendered: 'rendered' },
      idempotency_key: 'e2e-helper-map.template:free-action',
    });
    expect(post).toHaveBeenCalledTimes(1);
  });
});
