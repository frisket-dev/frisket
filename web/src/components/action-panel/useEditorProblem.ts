import { useEffect } from 'react';

/** Reports a body's blocking problem to the host and withdraws it on unmount. */
export function useEditorProblem(
  setEditorProblem: ((message: string | null) => void) | undefined,
  problem: string | null,
): void {
  useEffect(() => {
    setEditorProblem?.(problem);
    return () => setEditorProblem?.(null);
  }, [problem, setEditorProblem]);
}
