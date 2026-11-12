/** A fixed local page can request only startup actions, never paths or code. */
export function installStartupBridge({ ipcMain, pageUrl, findWindow, snapshot, retry, close, copy, details, resize }) {
  const trusted = event => {
    const window = findWindow(event.sender);
    return window && !window.isDestroyed() && event.senderFrame === event.sender.mainFrame
      && event.senderFrame.url === pageUrl ? window : undefined;
  };
  const handlers = {
    get: window => window.webContents.send('startup:state', snapshot(window)),
    retry, close, copy,
    resize: (window, height) => { if (Number.isFinite(height)) resize(window, Math.round(Math.max(240, Math.min(640, height)))); },
    details: (window, open) => { if (typeof open === 'boolean') details(window, open); },
  };
  for (const [name, handler] of Object.entries(handlers)) {
    ipcMain.on(`startup:${name}`, (event, argument) => {
      const window = trusted(event);
      if (window) handler(window, argument);
    });
  }
}

export function canRetryStartup({ visible, busy, cleanupFailed, quitting, retryable }) {
  return visible && !busy && !cleanupFailed && !quitting && retryable;
}

/** Fixed native windows can retain minimum/maximum constraints at their old size. */
export function resizeStartupWindow(window, height) {
  // The renderer measures CSS pixels; native window sizes use display-independent
  // pixels. Page zoom changes their ratio (Retina scaling is already handled by Electron).
  const zoom = window.webContents.getZoomFactor();
  window.setResizable(true);
  try {
    // Toggling resizability alone does not clear retained native size limits
    // on every platform. This card's bounds are owned by its content.
    window.setMinimumSize(0, 0);
    window.setMaximumSize(0, 0);
    window.setContentSize(Math.ceil(360 * zoom), Math.ceil(height * zoom));
  } finally {
    window.setResizable(false);
  }
}
