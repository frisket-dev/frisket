import type { ReactNode } from 'react';

import './ImportWorkspaceShared.css';

function classes(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(' ');
}

export function ImportDestinationOptions({
  children,
  className,
  legend = 'Import into',
}: {
  children: ReactNode;
  className?: string;
  legend?: ReactNode;
}) {
  return (
    <fieldset className={classes('import-destination-options', className)}>
      <legend>{legend}</legend>
      {children}
    </fieldset>
  );
}

export function ImportDestinationChoice({
  children,
  className,
  description,
  disabled = false,
  name,
  onSelect,
  selected,
  showInactiveChildren = false,
  title,
}: {
  children?: ReactNode;
  className?: string;
  description?: ReactNode;
  disabled?: boolean;
  name: string;
  onSelect(): void;
  selected: boolean;
  showInactiveChildren?: boolean;
  title: ReactNode;
}) {
  return (
    <div className={classes('import-destination-choice', selected && 'active', className)}>
      <label>
        <input
          type="radio"
          name={name}
          disabled={disabled}
          checked={selected}
          onChange={onSelect}
        />
        <span>
          <strong>{title}</strong>
          {description ? <small>{description}</small> : null}
        </span>
      </label>
      {selected || showInactiveChildren ? children : null}
    </div>
  );
}
