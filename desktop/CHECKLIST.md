# Frisket Desktop checklist

Reviewed against Electron's official guidance on 2026-09-12. This is the
macOS Apple Silicon beta checklist; installed-app evidence comes from the
`desktop-macos` workflow for the exact candidate revision.

| Area | Implementation and proof |
| --- | --- |
| Window close and Quit | Both use guarded shutdown and wait for Python descendants. The installed journey exercises native last-window close separately from `app.quit()`. [Lifecycle](https://www.electronjs.org/docs/latest/api/app#event-window-all-closed) |
| Profile continuity | Create and keep the original `Application Support/Frisket` directory across the Desktop rename; respect explicit test profiles. [App paths](https://www.electronjs.org/docs/latest/api/app#appsetpathname-path) |
| Downloads | Use Electron's save chooser options synchronously during `will-download`; installed export supplies a test destination at that same event. [DownloadItem](https://www.electronjs.org/docs/latest/api/download-item/) |
| Startup and recovery | Local CSP-safe progress follows real setup phases. Setup/backend failures offer Retry/Quit; main-renderer crashes use the same recovery owner. [Renderer exit](https://www.electronjs.org/docs/latest/api/web-contents#event-render-process-gone) |
| Window presentation | Explicit background, application title, Frisket icon, and restore/focus on a second launch. [Window background](https://www.electronjs.org/docs/latest/api/browser-window#setting-the-backgroundcolor-property), [icons](https://www.electron.build/v26/docs/features/icons-and-images/) |
| Renderer isolation | Sandbox and context isolation enabled; no Node, preload bridge, webview or relaxed web security. Permissions allow only app-origin clipboard writes. [Security checklist](https://www.electronjs.org/docs/latest/tutorial/security) |
| Navigation and service access | Restrict app navigation/popups, send external HTTP(S) links to the OS browser, and inject the private loopback token only in the main process. Installed checks exercise clipboard, popup and unauthenticated HTTP rejection. [Protocol](https://www.electronjs.org/docs/latest/api/protocol) |
| Framework updates | Pin Electron and review supported-version patch updates as part of releases. [Support policy](https://www.electronjs.org/docs/latest/tutorial/electron-timelines) |
| Beta installation | Deliver one DMG. Pull-request builds use ad-hoc signatures; the protected release workflow signs with Developer ID, notarizes and staples the DMG, and validates the installed app. Manual replacement retains data outside the bundle. |

Remaining distribution work: a signed update channel and package-time fuse
hardening with installed-app validation.
Disabling CLI inspection must be coordinated with the current Playwright
Electron harness. [Code signing](https://www.electronjs.org/docs/latest/tutorial/code-signing),
[fuses](https://www.electronjs.org/docs/latest/tutorial/fuses).

Later conveniences: remember window bounds, report data-folder open failures,
and prune superseded runtime environments. None requires a new runtime manager
for the beta.
