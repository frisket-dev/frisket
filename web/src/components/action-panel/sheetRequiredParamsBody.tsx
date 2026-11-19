import type { ComponentType } from 'react';

import type { SheetMeta } from '../../api/open';
import type { GeneratedActionParams } from '../../generated/actionTypes';
import type { GeneratedActionParamsBody, GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';

export type SheetBoundParamsBodyProps<K extends keyof GeneratedActionParams> =
  GeneratedActionParamsBodyProps<K> & { sheet: SheetMeta };

/** A Params editor that reads the source sheet's columns renders only once a
 * sheet is chosen. */
export function sheetRequired<K extends keyof GeneratedActionParams>(
  Body: ComponentType<SheetBoundParamsBodyProps<K>>,
): GeneratedActionParamsBody<K> {
  return function SheetRequiredParamsBody(props: GeneratedActionParamsBodyProps<K>) {
    if (!props.sheet) return <p className="form-hint">Choose a source sheet to configure this action.</p>;
    return <Body {...props} sheet={props.sheet} />;
  };
}
