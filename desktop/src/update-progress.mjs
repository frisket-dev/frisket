/** A desktop-owned modal: the workspace renderer has no updater privileges. */
export function createUpdateProgress({ BrowserWindow, getParent, pagePath, createQuitMenu }) {
  let window;
  let latest;
  let loaded = false;

  function render() {
    if (!loaded || !window || window.isDestroyed()) return;
    void window.webContents.executeJavaScript(
      `window.updateDownloadProgress(${JSON.stringify(latest)})`,
    ).catch(() => {});
  }

  function close() {
    const current = window;
    window = undefined;
    loaded = false;
    if (current && !current.isDestroyed()) current.destroy();
  }

  function update(state) {
    if (state.phase !== 'downloading') { close(); return; }
    latest = {
      percent: Number.isFinite(state.percent) ? Math.max(0, Math.min(100, state.percent)) : 0,
    };
    if (window && !window.isDestroyed()) { render(); return; }
    const parent = getParent();
    if (!parent || parent.isDestroyed()) return;
    window = new BrowserWindow({
      parent, modal: true, title: 'Updating Frisket Desktop',
      width: 460, height: 240, useContentSize: true,
      resizable: false, minimizable: false, maximizable: false, closable: false,
      show: true, backgroundColor: '#f9f7fc',
      webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false, webviewTag: false },
    });
    const current = window;
    // Windows disables the parent's menu while a modal is open. Keep its own
    // Quit command available so a stalled download never requires a force-kill.
    current.setMenu(createQuitMenu());
    // No close button, Escape, or window-manager close can hide an active download.
    current.on('close', (event) => event.preventDefault());
    current.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    current.webContents.on('will-navigate', (event) => event.preventDefault());
    void current.loadFile(pagePath).then(() => {
      if (window !== current || current.isDestroyed()) return;
      loaded = true;
      render();
    }).catch(() => { if (window === current) close(); });
  }

  return { update, close };
}
