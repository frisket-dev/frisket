import { APP_ORIGIN } from './protocol.mjs';

// Match the original string, before URL normalization can hide traversal,
// encoded separators, duplicate query parameters, or credentials.
export function launchIntent(value) {
  if (typeof value !== 'string') return null;
  const match = /^frisket:\/\/learn\/([a-z0-9]+(?:-[a-z0-9]+)*)\?revision=([1-9][0-9]*)$/.exec(value);
  if (!match || !Number.isSafeInteger(Number(match[2]))) return null;
  return `${APP_ORIGIN}/learn/${match[1]}?revision=${match[2]}`;
}

// One pending intent, with the most recent valid request taking precedence.
// Navigation is serialized and only starts after the startup window retires.
export function createLaunchIntents({ navigate }) {
  let pending;
  let ready = false;
  let navigating = false;
  async function drain() {
    if (!ready || navigating || !pending) return;
    const destination = pending;
    pending = undefined;
    navigating = true;
    try { await navigate(destination); } catch { /* startup/recovery may cancel a navigation */ }
    finally { navigating = false; void drain(); }
  }
  return {
    accept(value) {
      const destination = launchIntent(value);
      if (!destination) return false;
      pending = destination;
      void drain();
      return true;
    },
    acceptArgs(args) { for (const arg of args) this.accept(arg); },
    setReady(value) { ready = value; if (ready) void drain(); },
  };
}

// Installed before Electron's ready event so macOS cold-start delivery is kept.
export function installLaunchDelivery({ app, intents, argv, focus }) {
  app.on('open-url', (event, url) => { event.preventDefault(); intents.accept(url); });
  const ownsInstance = app.requestSingleInstanceLock();
  if (!ownsInstance) { app.quit(); return false; }
  intents.acceptArgs(argv);
  app.on('second-instance', (_event, args) => { intents.acceptArgs(args); focus(); });
  return true;
}
