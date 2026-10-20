import { useEffect, useMemo, useRef, useState } from 'react';
import { errMsg } from './settingsSupport';

type Submission = { name: string; value: string; revision: number };
type Session = { scope: unknown };
type Owner = { session: Session; revision: number; pending: Submission | null };
const initial = (session: Session, name: string) => ({
  session, name, value: '', saving: false, notice: null as string | null, error: null as string | null,
});

/** Secret values stay local. A completed write may clear only its submitted draft. */
export function useSecretEntryDraft(scope: unknown, initialName = '') {
  // A return to the same scope is a new session, not authority for an old completion.
  const session = useMemo(() => ({ scope }), [scope]);
  const owner = useRef<Owner | null>(null);
  const [state, setState] = useState(() => initial(session, initialName));
  const current = state.session === session ? state : initial(session, initialName);
  useEffect(() => {
    owner.current = { session, revision: 0, pending: null };
    return () => { owner.current = null; };
  }, [session]);

  const update = (patch: Partial<Pick<typeof current, 'name' | 'value'>>) => {
    if (!owner.current || owner.current.session !== session) return;
    owner.current.revision += 1;
    setState((previous) => ({ ...(previous.session === session ? previous : initial(session, initialName)), ...patch }));
  };
  const begin = () => {
    if (!owner.current || owner.current.session !== session || owner.current.pending || !current.name.trim() || !current.value) return null;
    const submission = { name: current.name, value: current.value, revision: owner.current.revision };
    owner.current.pending = submission;
    setState({ ...current, saving: true, error: null });
    return submission;
  };
  const settle = (submission: Submission, error: string | null) => {
    const active = owner.current;
    if (!active || active.session !== session || active.pending !== submission) return false;
    active.pending = null;
    setState((previous) => {
      if (previous.session !== session) return previous;
      return {
        ...previous, saving: false, error, notice: error === null ? 'Secret saved' : null,
        ...(error === null && active.revision === submission.revision ? { name: '', value: '' } : {}),
      };
    });
    return true;
  };
  return {
    name: current.name, value: current.value, saving: current.saving,
    notice: current.notice, error: current.error, begin,
    setName: (name: string) => update({ name }),
    setValue: (value: string) => update({ value }),
    complete: (submission: Submission) => settle(submission, null),
    fail: (submission: Submission, error: unknown) => settle(submission, errMsg(error)),
    setError: (error: string | null) => {
      if (owner.current?.session === session) setState((previous) => previous.session === session ? { ...previous, error } : previous);
    },
  };
}
