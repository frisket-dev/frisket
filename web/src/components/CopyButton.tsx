import { useEffect, useRef, useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { Button } from './Button';

/** Copy an already prepared value; callers still own assembling that value. */
interface CopyButtonProps {
  text: string; testId?: string; label?: string;
}
export function CopyButton(props: CopyButtonProps) {
  return <CopyControl key={props.text} {...props} />;
}
function CopyControl({ text, testId, label = 'copy' }: CopyButtonProps) {
  const [status, setStatus] = useState<'idle' | 'copied' | 'error'>('idle');
  const lifetime = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => {
    return () => {
      lifetime.current += 1;
      clearTimeout(timer.current);
    };
  }, []);
  const copy = async () => {
    const request = ++lifetime.current;
    clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(text);
      if (request !== lifetime.current) return;
      setStatus('copied');
      timer.current = setTimeout(() => setStatus('idle'), 1200);
    } catch {
      if (request === lifetime.current) setStatus('error');
    }
  };
  return <Button type="button" data-testid={testId}
    title={status === 'error' ? 'Could not copy. Please try again.' : 'Copy to clipboard'}
    onClick={() => void copy()}>
    {status === 'copied' ? <Check size={11} /> : <Copy size={11} />}
    {' '}{status === 'copied' ? 'copied' : status === 'error' ? 'retry copy' : label}
  </Button>;
}
