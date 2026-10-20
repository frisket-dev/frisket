import '../ActionPanel.css';
import { Button } from '../Button';
import { TextInput } from '../FormControls';
import { useState } from 'react';

/** Host naming convenience only: never computes the join's logical schema. */
export function JoinOutputNames({ outputs, names, onChange, disabled }: {
  outputs: readonly { key: string }[];
  names: Readonly<Record<string, string>>;
  onChange(names: Record<string, string>): void;
  disabled: boolean;
}) {
  const [left, setLeft] = useState('_left');
  const [right, setRight] = useState('_right');
  const apply = (side: 'left' | 'right', suffix: string) => {
    const pattern = new RegExp(`_${side}(_\\d+)?$`);
    onChange(Object.fromEntries(outputs.map(({ key }) => [key,
      pattern.test(key) ? key.replace(pattern, (_, occurrence: string | undefined) =>
        `${suffix}${occurrence ?? ''}`) : names[key] ?? key,
    ])));
  };
  return <details className="action-advanced" data-testid="join-output-naming">
    <summary>Bulk rename suffixed columns</summary>
    <p className="form-hint">Replace default _left or _right name endings. This updates the names below;
      you can still rename every column independently.</p>
    {(['left', 'right'] as const).map((side) => <div className="form-row-pair" key={side}>
      <label className="field-group form-field"><span className="form-label">{side === 'left' ? 'Left' : 'Right'} suffix</span>
        <TextInput data-testid={`field-join_${side}_suffix`} disabled={disabled}
          value={side === 'left' ? left : right} onChange={(event) =>
            (side === 'left' ? setLeft : setRight)(event.target.value)} /></label>
      <Button type="button" className="btn-ghost" disabled={disabled}
        onClick={() => apply(side, side === 'left' ? left : right)}>Apply {side} suffix</Button>
    </div>)}
  </details>;
}
