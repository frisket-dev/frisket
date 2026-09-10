import { describe, expect, it } from 'vitest';

import type { ActionLaunchWire } from '../../src/api/actionLaunch';
import type { ApiError } from '../../src/api/contractErrors';
import { normalizeV1ActionResult } from '../../src/api/v1ActionSession';

function actionResult(
  overrides: Partial<ActionLaunchWire> = {},
): ActionLaunchWire {
  return {
    schema_version: 'frisket.action_result.v1',
    action: { kind: 'map.classify', action_id: 'action-1' },
    status: 'completed',
    project_id: 'project-1',
    ...overrides,
  };
}

describe('v1 action result normalization', () => {
  it('fills only session defaults while preserving generated response fields', () => {
    const raw = {
      ...actionResult({
        op_ids: [3, 5],
        warnings: ['producer warning'],
        outputs: [{ kind: 'sheet', sheet_id: 7 }],
      }),
      producer_extension: { retained: true },
    };

    const normalized = normalizeV1ActionResult(raw);

    expect(normalized.errors).toEqual([]);
    expect(normalized.outputs).toEqual([{
      kind: 'sheet',
      name: null,
      sheet_id: 7,
    }]);
    expect(normalized.op_ids).toEqual([3, 5]);
    expect(normalized.warnings).toEqual(['producer warning']);
    expect((normalized as unknown as Record<string, unknown>).producer_extension).toEqual({
      retained: true,
    });
  });

  it('preserves rich generated errors and explicit output names', () => {
    const normalized = normalizeV1ActionResult(actionResult({
      status: 'failed',
      errors: [{
        code: 'action_failed',
        message: 'Action failed',
        field: 'params.model',
        details: { provider: 'example' },
      }],
      outputs: [{ kind: 'column', name: 'sentiment', column_id: 9 }],
    }));

    expect(normalized.errors).toEqual([{
      code: 'action_failed',
      message: 'Action failed',
      field: 'params.model',
      details: { provider: 'example' },
    }]);
    expect(normalized.outputs?.[0]?.name).toBe('sentiment');
  });

  it.each([undefined, 'frisket.action_result.v2'])(
    'rejects the %s schema discriminator',
    (schemaVersion) => {
      const malformed = {
        ...actionResult(),
        schema_version: schemaVersion,
      } as unknown as ActionLaunchWire;

      expect(() => normalizeV1ActionResult(malformed)).toThrow(
        expect.objectContaining<Partial<ApiError>>({
          name: 'ApiError',
          status: 500,
          message: 'Unexpected action result schema',
        }),
      );
    },
  );
});
