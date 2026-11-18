import type { APIRequestContext, APIResponse } from '@playwright/test';
import { describe, expect, test, vi } from 'vitest';

import { runAndWait, typedActionRequest } from '../e2e/helpers';

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
  test('typedActionRequest builds the registered request with a fresh retry identity', () => {
    const first = typedActionRequest('map.ner', {
      sheetId: 3,
      rowIds: [2, 4],
      params: { source: ['snippet'], labels: ['person'], engine: 'spacy' },
      outputNames: { entities: 'entities' },
    });
    const second = typedActionRequest('map.ner', {
      sheetId: 3,
      params: { source: ['snippet'], labels: ['person'], engine: 'spacy' },
    });

    expect(first).toEqual({
      action_id: 'map.ner',
      scope: { kind: 'sheet_rows', sheet_id: 3, row_ids: [2, 4] },
      params: { source: ['snippet'], labels: ['person'], engine: 'spacy' },
      output_names: { entities: 'entities' },
      idempotency_key: expect.stringMatching(/^e2e-map\.ner:/),
    });
    expect(second.scope).toEqual({ kind: 'sheet_rows', sheet_id: 3 });
    expect(second.output_names).toEqual({});
    expect(second.idempotency_key).not.toBe(first.idempotency_key);
    expect(typedActionRequest('row.add', { params: { sheet_id: 3, cells: {} } }).scope)
      .toEqual({ kind: 'project' });
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
