import { Component, StrictMode, type ErrorInfo, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
// IBM Plex, self-hosted via @fontsource (Vite bundles the woff2 — no Google
// Fonts / CDN dependency; the app renders offline). Sans for UI + data, Mono
// for labels/numerals/shortcuts.
import '@fontsource/ibm-plex-sans/400.css';
import '@fontsource/ibm-plex-sans/500.css';
import '@fontsource/ibm-plex-sans/600.css';
import '@fontsource/ibm-plex-sans/700.css';
import '@fontsource/ibm-plex-mono/400.css';
import '@fontsource/ibm-plex-mono/500.css';
import '@fontsource/ibm-plex-mono/600.css';
import '../styles.css';
import { PanelError } from '../components/PanelPrimitives';
import { createErrorIntakeApi } from '../api/errorIntake';
import { browserRouteContext } from '../routeContext';
import { applyStoredPersonalPreferences } from '../settings/preferences';
import {
  EditionModuleProvider,
  type EditionModule,
} from '../editions/module';

declare global {
  interface Window {
    __FRISKET_CLIENT_ERROR_CAPTURE__?: boolean | number | string | null;
    __FRISKET_RECENT_CLIENT_ERROR_IDS__?: number[];
  }
}

const CLIENT_ERROR_CAPTURE_SESSION_KEY = 'frisket:client-error-capture';
const CLIENT_ERROR_CAPTURE_QUERY_PARAM = 'frisket_client_errors';
const RECENT_CLIENT_ERROR_ID_LIMIT = 10;
const clientErrorIntakeApi = createErrorIntakeApi(
  (status) => new Error(`Client error report failed (HTTP ${status})`),
);

const truthyFlag = (value: unknown): boolean => {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value === 1;
  if (typeof value !== 'string') return false;
  return /^(1|true|yes|on)$/i.test(value.trim());
};

const falseyFlag = (value: unknown): boolean => {
  if (typeof value === 'boolean') return !value;
  if (typeof value === 'number') return value === 0;
  if (typeof value !== 'string') return false;
  return /^(0|false|no|off)$/i.test(value.trim());
};

const rememberClientErrorCaptureOptIn = (enabled: boolean): void => {
  try {
    if (enabled) {
      window.sessionStorage.setItem(CLIENT_ERROR_CAPTURE_SESSION_KEY, '1');
    } else {
      window.sessionStorage.removeItem(CLIENT_ERROR_CAPTURE_SESSION_KEY);
    }
  } catch {
    // Private browsing or blocked storage should not affect app startup.
  }
};

const hasRememberedClientErrorCaptureOptIn = (): boolean => {
  try {
    return window.sessionStorage.getItem(CLIENT_ERROR_CAPTURE_SESSION_KEY) === '1';
  } catch {
    return false;
  }
};

const localDebugOrigin = (): boolean => {
  const host = window.location.hostname.toLowerCase();
  return host === 'localhost'
    || host === '127.0.0.1'
    || host === '::1'
    || host.endsWith('.localhost');
};

const clientErrorCaptureEnabled = (): boolean => {
  if (truthyFlag(window.__FRISKET_CLIENT_ERROR_CAPTURE__)) return true;
  if (truthyFlag(import.meta.env.VITE_FRISKET_CLIENT_ERROR_CAPTURE)) return true;

  const params = new URLSearchParams(window.location.search);
  if (localDebugOrigin() && params.has(CLIENT_ERROR_CAPTURE_QUERY_PARAM)) {
    const value = params.get(CLIENT_ERROR_CAPTURE_QUERY_PARAM);
    if (truthyFlag(value)) {
      rememberClientErrorCaptureOptIn(true);
      return true;
    }
    if (falseyFlag(value)) {
      rememberClientErrorCaptureOptIn(false);
      return false;
    }
    return hasRememberedClientErrorCaptureOptIn();
  }

  return hasRememberedClientErrorCaptureOptIn();
};

const CLIENT_ERROR_CAPTURE_ENABLED = clientErrorCaptureEnabled();

const redactBrowserText = (value: unknown, limit = 4000): string => {
  let text = String(value ?? '');
  text = text.replace(/https?:\/\/[^\s)"']+/g, (raw) => {
    try {
      const url = new URL(raw);
      return `${url.origin}${url.pathname}`;
    } catch {
      return raw.split('?')[0];
    }
  });
  text = text.replace(
    /(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]+/gi,
    '$1[redacted]',
  );
  text = text.replace(
    /((?:api[_-]?key|token|secret|password|session)\s*[:=]\s*)[^\s,;&)"']+/gi,
    '$1[redacted]',
  );
  text = text.replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, '[redacted-key]');
  text = text.replace(/\bfrisket_pat_[A-Za-z0-9_-]{8,}\b/g, '[redacted-token]');
  text = text.replace(
    /(^|[^A-Za-z0-9_=-])([A-Za-z0-9_=-]{40,})(?=$|[^A-Za-z0-9_=-])/g,
    '$1[redacted-token]',
  );
  return text.length <= limit ? text : `${text.slice(0, limit - 3)}...`;
};

const rememberClientErrorId = (id: unknown): void => {
  const value = typeof id === 'number' ? id : Number(id);
  if (!Number.isInteger(value) || value <= 0) return;
  const current = Array.isArray(window.__FRISKET_RECENT_CLIENT_ERROR_IDS__)
    ? window.__FRISKET_RECENT_CLIENT_ERROR_IDS__
    : [];
  window.__FRISKET_RECENT_CLIENT_ERROR_IDS__ = [
    value,
    ...current.filter((existing) => existing !== value),
  ].slice(0, RECENT_CLIENT_ERROR_ID_LIMIT);
};

const reportBrowserError = (input: {
  source: string;
  name?: string | null;
  message: string;
  stack?: string | null;
  context?: Record<string, unknown>;
}): void => {
  if (!CLIENT_ERROR_CAPTURE_ENABLED) return;

  const route = window.location.pathname;
  const context = { ...browserRouteContext(), ...(input.context ?? {}) };
  const body = {
    source: input.source,
    severity: 'error',
    name: input.name ? redactBrowserText(input.name, 200) : null,
    message: redactBrowserText(input.message || '(no message)', 1000),
    stack: input.stack ? redactBrowserText(input.stack, 5000) : null,
    route,
    context,
  };
  clientErrorIntakeApi.reportClientError(body, {
    credentials: 'same-origin',
    keepalive: true,
  })
    .then((payload) => {
      rememberClientErrorId(payload.id);
    })
    .catch(() => undefined);
};

window.addEventListener('error', (event) => {
  const err = event.error instanceof Error ? event.error : null;
  reportBrowserError({
    source: 'browser',
    name: err?.name ?? 'ErrorEvent',
    message: event.message || err?.message || 'Unhandled browser error',
    stack: err?.stack ?? null,
  });
});

window.addEventListener('unhandledrejection', (event) => {
  const reason = event.reason;
  const err = reason instanceof Error ? reason : null;
  reportBrowserError({
    source: 'unhandledrejection',
    name: err?.name ?? 'UnhandledRejection',
    message: err?.message ?? String(reason ?? 'Unhandled promise rejection'),
    stack: err?.stack ?? null,
  });
});

class ErrorBoundary extends Component<
  { children: ReactNode },
  { crashed: boolean }
> {
  state = { crashed: false };

  static getDerivedStateFromError(): { crashed: boolean } {
    return { crashed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    reportBrowserError({
      source: 'react',
      name: error.name,
      message: error.message,
      stack: error.stack ?? null,
      context: { component_stack: info.componentStack },
    });
  }

  render(): ReactNode {
    if (this.state.crashed) {
      return <PanelError className="panel-loading-page">Something went wrong.</PanelError>;
    }
    return this.props.children;
  }
}

export function mountEdition(
  edition: Readonly<EditionModule>,
  node: ReactNode,
): void {
  const root = document.getElementById('root');
  if (!root) throw new Error('Missing #root mount point');
  applyStoredPersonalPreferences();
  createRoot(root).render(
    <StrictMode>
      <ErrorBoundary>
        <EditionModuleProvider edition={edition}>{node}</EditionModuleProvider>
      </ErrorBoundary>
    </StrictMode>,
  );
}
