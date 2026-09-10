import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  createProjectContract,
  deleteProjectContract,
  getProjectContract,
  updateProjectContract,
} from '../../src/api/httpContractRoutes';

const projectWire = {
  id: 'project-1',
  name: 'Project One',
  description: '',
  sensitive: false,
  starred: false,
  archived: false,
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function contractError(status: number, payload: unknown): Error {
  return new Error(`unexpected contract error ${status}: ${JSON.stringify(payload)}`);
}

class MappedContractError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super(`mapped contract error ${status}`);
    this.name = 'MappedContractError';
    this.status = status;
    this.payload = payload;
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('project CRUD HTTP contracts', () => {
  it('percent-encodes project path parameters', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(projectWire);
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      getProjectContract('tenant/project 1', contractError),
    ).resolves.toMatchObject({ id: 'project-1' });

    expect(requests).toHaveLength(1);
    expect(requests[0]?.input).toBe('/api/projects/tenant%2Fproject%201');
    expect(requests[0]?.init?.method).toBe('GET');
  });

  it('sends the declared JSON body with DELETE', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse({ ok: true, deleted: 'project-1' });
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      deleteProjectContract('project/1', 'Project One', contractError),
    ).resolves.toBeUndefined();

    expect(requests).toHaveLength(1);
    expect(requests[0]?.input).toBe('/api/projects/project%2F1');
    expect(requests[0]?.init?.method).toBe('DELETE');
    expect(requests[0]?.init?.body).toBe(JSON.stringify({
      confirm_name: 'Project One',
    }));
    expect(new Headers(requests[0]?.init?.headers).get('content-type')).toBe(
      'application/json',
    );
  });

  it('retries one exact project-deletion lifetime challenge', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      if (requests.length === 1) {
        return jsonResponse({
          detail: 'project deletion confirmation is bound to the current project lifetime',
          confirmation: {
            action: 'project.delete',
            project_id: 'project/1',
            project_row_id: 41,
            confirm_name: 'Project One',
          },
        }, 422);
      }
      return jsonResponse({ ok: true, deleted: 'project/1' });
    });
    vi.stubGlobal('fetch', fetchMock);
    const errorFactory = vi.fn(contractError);

    await expect(
      deleteProjectContract('project/1', 'Project One', errorFactory),
    ).resolves.toBeUndefined();

    expect(requests).toHaveLength(2);
    expect(requests[0]?.input).toBe('/api/projects/project%2F1');
    expect(requests[0]?.init?.body).toBe(JSON.stringify({
      confirm_name: 'Project One',
    }));
    expect(requests[1]?.input).toBe('/api/projects/project%2F1');
    expect(requests[1]?.init?.body).toBe(JSON.stringify({
      confirm_name: 'Project One',
      project_row_id: 41,
    }));
    expect(errorFactory).not.toHaveBeenCalled();
  });

  it('propagates a malformed project-deletion lifetime challenge without retrying', async () => {
    const malformed = {
      detail: 'project deletion confirmation is bound to the current project lifetime',
      confirmation: {
        action: 'project.delete',
        project_id: 'project/1',
        project_row_id: 41,
        confirm_name: 'Project One',
        unexpected: true,
      },
    };
    const fetchMock = vi.fn(async () => jsonResponse(malformed, 422));
    vi.stubGlobal('fetch', fetchMock);
    const errorFactory = vi.fn(
      (status: number, payload: unknown) => new MappedContractError(status, payload),
    );

    await expect(
      deleteProjectContract('project/1', 'Project One', errorFactory),
    ).rejects.toMatchObject({
      name: 'MappedContractError',
      status: 422,
      payload: malformed,
    });

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(errorFactory).toHaveBeenCalledOnce();
  });

  it('propagates a mismatched project-deletion lifetime challenge without retrying', async () => {
    const mismatched = {
      detail: 'project deletion confirmation is bound to the current project lifetime',
      confirmation: {
        action: 'project.delete',
        project_id: 'other-project',
        project_row_id: 41,
        confirm_name: 'Project One',
      },
    };
    const fetchMock = vi.fn(async () => jsonResponse(mismatched, 422));
    vi.stubGlobal('fetch', fetchMock);
    const errorFactory = vi.fn(
      (status: number, payload: unknown) => new MappedContractError(status, payload),
    );

    await expect(
      deleteProjectContract('project/1', 'Project One', errorFactory),
    ).rejects.toMatchObject({
      name: 'MappedContractError',
      status: 422,
      payload: mismatched,
    });

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(errorFactory).toHaveBeenCalledOnce();
  });

  it('propagates a second project-deletion lifetime challenge without looping', async () => {
    const challenge = {
      detail: 'project deletion confirmation is bound to the current project lifetime',
      confirmation: {
        action: 'project.delete',
        project_id: 'project/1',
        project_row_id: 41,
        confirm_name: 'Project One',
      },
    };
    const fetchMock = vi.fn(async () => jsonResponse(challenge, 422));
    vi.stubGlobal('fetch', fetchMock);
    const errorFactory = vi.fn(
      (status: number, payload: unknown) => new MappedContractError(status, payload),
    );

    await expect(
      deleteProjectContract('project/1', 'Project One', errorFactory),
    ).rejects.toMatchObject({
      name: 'MappedContractError',
      status: 422,
      payload: challenge,
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(errorFactory).toHaveBeenCalledOnce();
  });

  it('maps declared and additional non-2xx statuses', async () => {
    const responses = [
      jsonResponse({ detail: 'project not found' }, 404),
      jsonResponse({ detail: 'teapot' }, 418),
    ];
    const fetchMock = vi.fn(async () => responses.shift()!);
    vi.stubGlobal('fetch', fetchMock);
    const errorFactory = vi.fn(
      (status: number, payload: unknown) => new MappedContractError(status, payload),
    );

    await expect(
      updateProjectContract('project-1', { name: 'Renamed' }, errorFactory),
    ).rejects.toMatchObject({
      name: 'MappedContractError',
      status: 404,
      payload: { detail: 'project not found' },
    });
    expect(errorFactory).toHaveBeenCalledOnce();

    await expect(
      updateProjectContract('project-1', { name: 'Renamed' }, errorFactory),
    ).rejects.toMatchObject({
      name: 'MappedContractError',
      status: 418,
      payload: { detail: 'teapot' },
    });
    expect(errorFactory).toHaveBeenCalledTimes(2);
    expect(errorFactory).toHaveBeenLastCalledWith(418, { detail: 'teapot' });
  });

  it('merges caller headers with JSON content type and honors an explicit override', async () => {
    const requestInits: RequestInit[] = [];
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      requestInits.push(init ?? {});
      return jsonResponse(projectWire);
    });
    vi.stubGlobal('fetch', fetchMock);

    await createProjectContract('Project One', contractError, {
      headers: { Authorization: 'Bearer token', 'X-Trace-Id': 'trace-1' },
    });
    await createProjectContract('Project One', contractError, {
      headers: new Headers({
        'content-type': 'application/vnd.frisket+json',
        'x-trace-id': 'trace-2',
      }),
    });

    const mergedHeaders = new Headers(requestInits[0]?.headers);
    expect(mergedHeaders.get('authorization')).toBe('Bearer token');
    expect(mergedHeaders.get('x-trace-id')).toBe('trace-1');
    expect(mergedHeaders.get('content-type')).toBe('application/json');

    const overriddenHeaders = new Headers(requestInits[1]?.headers);
    expect(overriddenHeaders.get('x-trace-id')).toBe('trace-2');
    expect(overriddenHeaders.get('content-type')).toBe('application/vnd.frisket+json');
  });

  it('forwards the exact signal and preserves abort rejection', async () => {
    const controller = new AbortController();
    let forwardedSignal: AbortSignal | null | undefined;
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      forwardedSignal = init?.signal;
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), {
          once: true,
        });
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const request = createProjectContract('Project One', contractError, {
      signal: controller.signal,
    });
    const abortError = new DOMException('cancelled', 'AbortError');
    controller.abort(abortError);

    await expect(request).rejects.toBe(abortError);
    expect(forwardedSignal).toBe(controller.signal);
  });
});
