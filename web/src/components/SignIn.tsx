import { useEffect, useRef, useState, type FormEvent } from 'react';
import { KeyRound, Mail } from 'lucide-react';
import { getRuntimeConfig, type RuntimeConfig } from '../api/open';
import { oidcSignInUrl } from '../api/raw/browserAuth';
import {
  completeMagicLink,
  completeProjectInvite,
  requestMagicLink,
  signInWithPassword,
} from '../api/browserAuth';
import { useInstanceIdentity } from '../instanceIdentity';
import { useEditionModule } from '../editions/module';
import { AuthForm, AuthScreen } from './AuthScreen';
import styles from './SignIn.module.css';

export function SignIn() {
  const { routes } = useEditionModule();
  const instance = useInstanceIdentity();
  const [config, setConfig] = useState<RuntimeConfig | null>(null);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [passwordConfirmation, setPasswordConfirmation] = useState('');
  const [magicLinkOpen, setMagicLinkOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const magicEmailRef = useRef<HTMLInputElement>(null);
  const [invitedArrival] = useState(
    () => new URLSearchParams(window.location.search).get('invited') === '1',
  );
  const [firstUse] = useState(() => {
    const query = new URLSearchParams(window.location.search);
    const kind = query.get('auth_kind');
    const token = query.get('auth_token');
    return token && (kind === 'magic-link' || kind === 'project-invite')
      ? { kind, token }
      : null;
  });
  const discoverableRoutes = routes.filter(
    (route) => route.discoverableFrom === 'sign-in',
  );

  useEffect(() => {
    if (!invitedArrival) return;
    const url = new URL(window.location.href);
    url.searchParams.delete('invited');
    window.history.replaceState({}, '', url.toString());
  }, [invitedArrival]);

  useEffect(() => {
    let alive = true;
    getRuntimeConfig()
      .then((runtime) => { if (alive) setConfig(runtime); })
      .catch(() => { if (alive) setError('Sign-in is temporarily unavailable.'); });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    if (magicLinkOpen) magicEmailRef.current?.focus();
  }, [magicLinkOpen]);

  async function submitPassword(event: FormEvent) {
    event.preventDefault();
    if (!email.trim() || !password || busy) return;
    setBusy(true);
    setError(null);
    try {
      await signInWithPassword(email, password);
      window.location.assign('/');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function submitMagicLink(event: FormEvent) {
    event.preventDefault();
    if (!email.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await requestMagicLink(email);
      setSent(true);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function completeFirstUse(event: FormEvent) {
    event.preventDefault();
    if (!firstUse || busy) return;
    if (password !== passwordConfirmation) {
      setError('Passwords do not match.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (firstUse.kind === 'magic-link') {
        await completeMagicLink(firstUse.token, password);
      } else {
        await completeProjectInvite(firstUse.token, password);
      }
      window.location.assign('/');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  const methods = config?.auth_methods;
  const hasPrimaryMethods = Boolean(methods?.password || methods?.oidc.length);

  if (firstUse) {
    return (
      <AuthScreen heading="Create your password" headingId="sign-in-heading" testId="sign-in">
          <p className={styles.intro}>Use this password whenever you return. You can still request an email link.</p>
          <AuthForm onSubmit={(event) => void completeFirstUse(event)}>
            <label>
              <span>Password</span>
              <input
                className={`form-input ${styles.control}`}
                type="password"
                autoComplete="new-password"
                minLength={12}
                maxLength={1024}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                required
                autoFocus
              />
            </label>
            <label>
              <span>Confirm password</span>
              <input
                className={`form-input ${styles.control}`}
                type="password"
                autoComplete="new-password"
                minLength={12}
                maxLength={1024}
                value={passwordConfirmation}
                onChange={(event) => setPasswordConfirmation(event.target.value)}
                required
              />
            </label>
            <button className={`btn btn-primary ${styles.control}`} type="submit" disabled={busy}>
              {busy ? 'Finishing…' : 'Create password and continue'}
            </button>
          </AuthForm>
          {error && <p className={styles.error} role="alert">{error}</p>}
      </AuthScreen>
    );
  }

  return (
    <AuthScreen heading="Sign in" headingId="sign-in-heading" testId="sign-in">
        <p className={styles.intro} data-testid="sign-in-intro">
          {invitedArrival
            ? 'That invite link is no longer available. Ask for a fresh link below.'
            : `Continue to ${instance.display_name}.`}
        </p>

        {methods?.oidc.map((provider) => (
          <a
            className={`btn ${styles.control}`}
            href={oidcSignInUrl(provider.id)}
            key={provider.id}
            data-testid={`sign-in-oidc-${provider.id}`}
          >
            Continue with {provider.label}
          </a>
        ))}

        {methods?.password && (
          <AuthForm onSubmit={(event) => void submitPassword(event)}>
            {methods.oidc.length > 0 && <div className={styles.divider}><span>or</span></div>}
            <label>
              <span>Email</span>
              <input
                className={`form-input ${styles.control}`}
                type="email"
                autoComplete="username"
                maxLength={320}
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                data-testid="sign-in-email"
                required
              />
            </label>
            <label>
              <span>Password</span>
              <input
                className={`form-input ${styles.control}`}
                type="password"
                autoComplete="current-password"
                maxLength={1024}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                data-testid="sign-in-password"
                required
              />
            </label>
            <button className={`btn btn-primary ${styles.control}`} type="submit" disabled={busy}>
              <KeyRound size={14} aria-hidden="true" />
              {busy ? 'Signing in…' : 'Sign in'}
            </button>
          </AuthForm>
        )}

        {methods?.magic_link && (
          <div className={styles.alternative}>
            {hasPrimaryMethods && (
              <button
                className={styles.linkButton}
                type="button"
                aria-expanded={magicLinkOpen}
                onClick={() => setMagicLinkOpen((open) => !open)}
                data-testid="sign-in-magic-toggle"
              >
                Email me a sign-in link instead
              </button>
            )}
            {(magicLinkOpen || !hasPrimaryMethods) && (
              sent ? (
                <p className={styles.status} role="status" data-testid="sign-in-sent">
                  Check your email. If <strong>{email}</strong> has access, a sign-in link
                  {config?.email_from_address ? <> from <strong>{config.email_from_address}</strong></> : null} is on its way.
                </p>
              ) : (
                <AuthForm onSubmit={(event) => void submitMagicLink(event)}>
                  <label>
                    <span>Email</span>
                    <input
                      ref={magicEmailRef}
                      className={`form-input ${styles.control}`}
                      type="email"
                      autoComplete="email"
                      maxLength={320}
                      value={email}
                      onChange={(event) => setEmail(event.target.value)}
                      required
                    />
                  </label>
                  <button type="submit" className={`btn ${styles.control}`} data-testid="sign-in-submit" disabled={busy}>
                    <Mail size={14} aria-hidden="true" /> {busy ? 'Sending…' : 'Send sign-in link'}
                  </button>
                </AuthForm>
              )
            )}
          </div>
        )}

        {error && <p className={styles.error} role="alert">{error}</p>}
        <div className={styles.accessLinks}>
          {discoverableRoutes.map((route) => (
            <a key={route.id} href={route.path} data-testid={route.stableHook ?? `sign-in-${route.id}`}>
              {route.linkLabel}
            </a>
          ))}
        </div>
    </AuthScreen>
  );
}
