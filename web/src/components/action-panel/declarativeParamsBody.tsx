import '../FormControls.css';
import '../ActionPanel.css';
import { Fragment, useEffect, type ReactNode } from 'react';

import type { GeneratedActionParams } from '../../generated/actionTypes';
import type { GeneratedActionParamsBody, GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';

type FieldName<K extends keyof GeneratedActionParams> = Extract<keyof GeneratedActionParams[K], string>;
type FieldItem<K extends keyof GeneratedActionParams> = {
  field: FieldName<K>;
  label?: string;
  testId?: string;
  hint?: ReactNode;
  hintTestId?: string;
  when?(props: GeneratedActionParamsBodyProps<K>): boolean;
  clearWhenHidden?: true;
};

export type DeclarativeParamsItem<K extends keyof GeneratedActionParams> =
  | FieldName<K>
  | FieldItem<K>
  | { hint: ReactNode; testId?: string; when?(props: GeneratedActionParamsBodyProps<K>): boolean }
  | { advanced: string; testId?: string; items: readonly DeclarativeParamsItem<K>[] }
  | ((props: GeneratedActionParamsBodyProps<K>) => ReactNode);

function renderItems<K extends keyof GeneratedActionParams>(
  items: readonly DeclarativeParamsItem<K>[],
  props: GeneratedActionParamsBodyProps<K>,
): ReactNode {
  const { Field } = props;
  return items.map((item, index) => {
    if (typeof item === 'string') return <Field key={index} name={item} />;
    if (typeof item === 'function') return <Fragment key={index}>{item(props)}</Fragment>;
    if ('advanced' in item) {
      return <details key={index} className="action-advanced" data-testid={item.testId}>
        <summary>{item.advanced}</summary>
        {renderItems(item.items, props)}
      </details>;
    }
    if (item.when && !item.when(props)) return null;
    if ('field' in item) {
      return item.hint === undefined
        ? <Field key={index} name={item.field} label={item.label} testId={item.testId} />
        : <Fragment key={index}>
            <Field name={item.field} label={item.label} testId={item.testId} />
            <p className="form-hint" data-testid={item.hintTestId}>{item.hint}</p>
          </Fragment>;
    }
    return <p key={index} className="form-hint" data-testid={item.testId}>{item.hint}</p>;
  });
}

function collectClearable<K extends keyof GeneratedActionParams>(
  items: readonly DeclarativeParamsItem<K>[],
): FieldItem<K>[] {
  return items.flatMap((item) => (
    typeof item !== 'object' ? []
      : 'advanced' in item ? collectClearable(item.items)
      : 'field' in item && item.clearWhenHidden && item.when ? [item] : []
  ));
}

/** Builds a Params body from a declarative sequence of schema fields, hints,
 * disclosure groups, and occasional inline custom controls. */
export function declarativeParamsBody<K extends keyof GeneratedActionParams>(
  items: readonly DeclarativeParamsItem<K>[],
): GeneratedActionParamsBody<K> {
  const clearable = collectClearable(items);
  return function DeclarativeParamsBody(props: GeneratedActionParamsBodyProps<K>) {
    const { params, setParams } = props;
    const hidden = clearable
      .filter((item) => !item.when!(props) && Object.prototype.hasOwnProperty.call(params, item.field))
      .map((item) => item.field)
      .join('\0');
    useEffect(() => {
      if (!hidden) return;
      const next = { ...params };
      for (const field of hidden.split('\0') as FieldName<K>[]) delete next[field];
      setParams(next);
    }, [hidden, params, setParams]);
    return <>{renderItems(items, props)}</>;
  };
}
