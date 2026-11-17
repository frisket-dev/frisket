import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  createProject,
  deleteProject,
  getProjectContract,
  updateProject,
} from '../../src/api/httpContractRoutes/projects';
import { ApiError } from '../../src/api/contractErrors';

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
      getProjectContract('tenant/project 1'),
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
      deleteProject('project/1', 'Project One'),
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

    await expect(
      deleteProject('project/1', 'Project One'),
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

    const request = deleteProject('project/1', 'Project One');
    await expect(request).rejects.toBeInstanceOf(ApiError);
    await expect(request).rejects.toMatchObject({
      name: 'ApiError',
      status: 422,
      message: malformed.detail,
      code: undefined,
      details: undefined,
    });

    expect(fetchMock).toHaveBeenCalledOnce();
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

    const request = deleteProject('project/1', 'Project One');
    await expect(request).rejects.toBeInstanceOf(ApiError);
    await expect(request).rejects.toMatchObject({
      name: 'ApiError',
      status: 422,
      message: mismatched.detail,
      code: undefined,
      details: undefined,
    });

    expect(fetchMock).toHaveBeenCalledOnce();
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

    const request = deleteProject('project/1', 'Project One');
    await expect(request).rejects.toBeInstanceOf(ApiError);
    await expect(request).rejects.toMatchObject({
      name: 'ApiError',
      status: 422,
      message: challenge.detail,
      code: undefined,
      details: undefined,
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('maps declared and additional non-2xx statuses', async () => {
    const responses = [
      jsonResponse({ detail: 'project not found' }, 404),
      jsonResponse({ detail: 'teapot' }, 418),
    ];
    const fetchMock = vi.fn(async () => responses.shift()!);
    vi.stubGlobal('fetch', fetchMock);

    const declared = updateProject('project-1', { name: 'Renamed' });
    await expect(declared).rejects.toBeInstanceOf(ApiError);
    await expect(declared).rejects.toMatchObject({
      name: 'ApiError',
      status: 404,
      message: 'project not found',
      code: undefined,
      details: undefined,
    });

    const additional = updateProject('project-1', { name: 'Renamed' });
    await expect(additional).rejects.toBeInstanceOf(ApiError);
    await expect(additional).rejects.toMatchObject({
      name: 'ApiError',
      status: 418,
      message: 'teapot',
      code: undefined,
      details: undefined,
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('sends the create body as JSON with only the JSON content type header', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(projectWire);
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(createProject('Project One')).resolves.toMatchObject({
      id: 'project-1',
    });

    expect(requests).toHaveLength(1);
    expect(requests[0]?.input).toBe('/api/projects');
    expect(requests[0]?.init?.method).toBe('POST');
    expect(requests[0]?.init?.body).toBe(JSON.stringify({ name: 'Project One' }));
    expect([...new Headers(requests[0]?.init?.headers).entries()]).toEqual([
      ['content-type', 'application/json'],
    ]);
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

    const request = createProject('Project One', { signal: controller.signal });
    const abortError = new DOMException('cancelled', 'AbortError');
    controller.abort(abortError);

    await expect(request).rejects.toBe(abortError);
    expect(forwardedSignal).toBe(controller.signal);
  });
});
