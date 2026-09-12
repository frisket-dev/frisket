# Frisket Desktop beta

macOS 14 or newer, Apple Silicon. Electron displays the ordinary local Frisket UI.
First launch downloads a private Python interpreter, the locked standard Python
dependencies, and Chromium for browser actions. It needs an internet connection.
The default local Whisper, Parakeet, VAD and RapidOCR model cache is included
with the app and copied into the preserved cache during setup. There is no system
Python, Homebrew, Node, or terminal setup for an app user.

## Install or update the beta

1. Open the successful desktop workflow run linked from the desktop pull request.
   Download its `Frisket-Desktop-macos-arm64-<sha>` artifact from **Artifacts** (GitHub
   sign-in required), then unzip the download.
2. Open the included DMG, drag **Frisket Desktop** into **Applications**, and eject the DMG.
3. Open Frisket Desktop from Applications. If macOS blocks it, open **System Settings →
   Privacy & Security → Open Anyway**, authenticate, and choose **Open**.
4. Leave Frisket Desktop open and connected to the internet while **Preparing Frisket**
   installs its runtime. The included local transcription and OCR models are ready
   after setup; optional models may download later when selected.

To update, quit Frisket Desktop with **Cmd-Q** or close its last window, open the new DMG, drag Frisket Desktop into
Applications, choose **Replace**, and reopen it. Your workspace and caches survive
replacement; a dependency change may require another download. If your older beta
is named `Frisket.app`, quit it and remove that old application bundle after
installing `Frisket Desktop.app`; both use the same preserved workspace folder.

Pull-request artifacts are ad-hoc signed test builds. A maintainer can manually
dispatch the protected `desktop-signing` environment to produce a Developer ID
signed and notarized DMG for the selected commit. Signed builds still update
manually using the steps above; there is no signed update feed.

## Signed macOS distribution

The dispatch-only signing job is the release-candidate path. It receives its
certificate and App Store Connect API key only from the protected GitHub
environment, never from pull requests. `electron-builder` signs the app with
the Developer ID Application certificate, enables the hardened runtime, and
signs the DMG container. The job notarizes and staples that final DMG once,
then mounts it to verify the contained app with `codesign` and checks the
distribution with `syspolicy_check`, `spctl`, and `xcrun stapler validate`.

The environment supplies `CSC_LINK` as a base64 `.p12`; `CSC_KEY_PASSWORD` is
optional, so the current passwordless certificate is supported and a protected
replacement can be used later. It also provides raw `APPLE_API_KEY_P8`,
`APPLE_API_KEY_ID`, and `APPLE_API_ISSUER`. The API key is written under the
runner temporary directory with owner-only permissions and removed at the end
of the signing step. `electron-builder` manages its own temporary certificate
keychain.

## Build

On an Apple Silicon Mac, install Node 22, Python 3.12+, and uv 0.11.29 as build tools:

```sh
npm --prefix sdk ci
npm --prefix sdk run build
npm --prefix web ci
npm --prefix desktop ci
python3 desktop/scripts/prepare.py
npm --prefix desktop run package:mac
```

`desktop/dist/` contains the DMG installer. The tester artifact contains only that DMG. The resource builder uses the ordinary
Frisket wheel, checks executable package resources, exports the exact standard
lock with hashes, and verifies every pinned native archive. See `THIRD_PARTY.md`.
The CI desktop workflow builds and tests these same artifacts.

## Ownership and storage

- Electron owns the window and one Python service. The renderer is sandboxed and
  has no Node API or preload bridge. A stable `frisket://app` origin preserves
  browser preferences across launches.
- Electron proxies that origin to a private loopback service, adding an
  unpredictable session token. The token travels to Python over stdin and never
  reaches renderer JavaScript. Every backend route requires it.
- Python owns the ordinary queue worker through the same process guardian used
  by CLI workers. Closing the last window or choosing Quit waits for cleanup, including during first-use setup.
- App resources contain Frisket code, the UI, the dependency lock and small native
  tools. They are replaced as a unit.
- `~/Library/Application Support/Frisket/` holds the workspace, private Python,
  immutable completed dependency environments, browser preferences and caches.
  The directory retains the original beta name across the Frisket Desktop rename.
  File → Open Data Folder opens it. A dependency change creates a new environment;
  an app-only change reuses the existing one. Incomplete installation retries at
  the same final path; environments are never moved after creation.
- Heavy models use the existing artifact-download service; download UI coverage varies by engine. There is no new
  model manager or always-on sidecar service.

## Tests

`npm --prefix desktop test` exercises the token/proxy boundary and real process
cancellation. Python tests cover the service authentication/readiness boundary.
The macOS job mounts the actual DMG, copies the app out, warms download caches,
then removes the dependency environment and seeded model caches. A temporary macOS PF rule blocks new
external connections while preserving the runner’s existing control connection.
This avoids nesting Seatbelt around Chromium’s own sandbox. The UI smoke uses no
providers or live datasets. The real application rebuilds its private environment
from the cache, imports CSV, runs a local action through its queue, exports, quits
and reopens persisted work. Native checks run Whisper, Parakeet, and RapidOCR from
the bundled model seed; model downloads occur only before packaging. Browser and
subprocess cleanup are checked.

Download-cache preparation is a networked build/setup step, separate from tests.
It is not evidence of offline first launch. The automated journey proves the
installed runtime and common UI path, not every optional engine or macOS dialog.

The [desktop checklist](CHECKLIST.md) records the Electron lifecycle, security and release checks.
