const { contextBridge, ipcRenderer } = require('electron');

// The main process also checks the exact local page and top frame on every call.
if (window.location.protocol === 'file:') {
  contextBridge.exposeInMainWorld('frisketStartup', {
    subscribe(callback) {
      if (typeof callback !== 'function') return () => {};
      const listener = (_event, state) => callback(state);
      ipcRenderer.on('startup:state', listener);
      ipcRenderer.send('startup:get');
      return () => ipcRenderer.removeListener('startup:state', listener);
    },
    retry: () => ipcRenderer.send('startup:retry'),
    close: () => ipcRenderer.send('startup:close'),
    copyDiagnostics: () => ipcRenderer.send('startup:copy'),
    resize: height => { if (Number.isFinite(height)) ipcRenderer.send('startup:resize', height); },
    details: open => { if (typeof open === 'boolean') ipcRenderer.send('startup:details', open); },
  });
}
