import type { ReactNode } from 'react';
import { Button } from '../components/Button';
import type { ValidateThenSaveBusy } from './useValidateThenSave';

/** Compact forms submit through one Test/Save action; full forms show Test and Save, then any extra actions. */
export function ValidateThenSaveButtons({ compact, busy, hasReceipt, ready, onTest, children }: {
  compact: boolean; busy: ValidateThenSaveBusy | null; hasReceipt: boolean; ready: boolean; onTest(): void; children?: ReactNode;
}) {
  if (compact) {
    const actionLabel = busy === 'test' ? 'Testing…' : busy === 'save' ? 'Saving…' : hasReceipt ? 'Save' : 'Test';
    return <Button className="btn-primary" type="submit" disabled={Boolean(busy) || !ready}>{actionLabel}</Button>;
  }
  return <>
    <Button type="button" disabled={Boolean(busy) || !ready} onClick={onTest}>{busy === 'test' ? 'Testing…' : 'Test'}</Button>
    <Button className="btn-primary" type="submit" disabled={Boolean(busy) || !hasReceipt || !ready}>{busy === 'save' ? 'Saving…' : 'Save'}</Button>
    {children}
  </>;
}
