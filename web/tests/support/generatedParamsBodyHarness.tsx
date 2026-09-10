import { useCallback, useState } from 'react';

import type { SheetMeta } from '../../src/api/types';
import type { GeneratedActionParamsBody } from '../../src/components/action-panel/GeneratedActionParamsBody';
import type { GeneratedActionParams } from '../../src/generated/actionTypes';

export function ParamsBodyHarness<K extends keyof GeneratedActionParams>({
  Body,
  sheet,
  initialParams,
  onParams,
}: {
  Body: GeneratedActionParamsBody<K>;
  sheet: SheetMeta;
  initialParams: GeneratedActionParams[K];
  onParams(params: GeneratedActionParams[K]): void;
}) {
  const [params, setParams] = useState(initialParams);
  const updateParams = useCallback((next: GeneratedActionParams[K]) => {
    setParams(next);
    onParams(next);
  }, [onParams]);
  const Field = useCallback(({ name, testId }: {
    name: Extract<keyof GeneratedActionParams[K], string>;
    testId?: string;
  }) => (
    <label>
      Column
      <select data-testid={testId ?? `field-${name}`} value={String(params[name] ?? '')}
        onChange={(event) => updateParams({ ...params, [name]: event.target.value })}>
        {sheet.columns.filter((column) => ['text', 'category', 'link'].includes(column.type))
          .map((column) => <option key={column.id} value={column.name}>{column.name}</option>)}
      </select>
    </label>
  ), [params, sheet.columns, updateParams]);
  return <Body sheet={sheet} params={params} setParams={updateParams} errors={{}} Field={Field} />;
}
