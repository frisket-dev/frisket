# Frisket desktop beta

macOS 14 or newer, Apple Silicon. Electron displays the ordinary local Frisket UI.
First launch downloads a private Python interpreter, the locked standard Python
dependencies, and Chromium for browser actions. It needs an internet connection.
There is no system Python, Homebrew, Node, or terminal setup for an app user.

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

`desktop/dist/` contains a DMG and ZIP. The resource builder uses the ordinary
Frisket wheel, checks executable package resources, exports the exact standard
lock with hashes, and verifies every pinned native archive. See `THIRD_PARTY.md`.
The CI desktop workflow builds and tests these same artifacts.

The current beta is ad-hoc signed and not notarized. It is a test distribution,
not an automatic update release. Gatekeeper may require an explicit Open Anyway
in macOS Privacy & Security. Replace the app while it is closed to upgrade this
beta. The workspace and caches are outside the app and survive replacement.
Developer ID signing, notarization and a signed update feed remain release work;
this build does not claim to test them.

## Ownership and storage

- Electron owns the window and one Python service. The renderer is sandboxed and
  has no Node API or preload bridge. A stable `frisket://app` origin preserves
  browser preferences across launches.
- Electron proxies that origin to a private loopback service, adding an
  unpredictable session token. The token travels to Python over stdin and never
  reaches renderer JavaScript. Every backend route requires it.
- Python owns the ordinary queue worker through the same process guardian used
  by CLI workers. Quit waits for cleanup, including during first-use setup.
- App resources contain Frisket code, the UI, the dependency lock and small native
  tools. They are replaced as a unit.
- `~/Library/Application Support/Frisket/` holds the workspace, private Python,
  immutable completed dependency environments, browser preferences and caches.
  File → Open Data Folder opens it. A dependency change creates a new environment;
  an app-only change reuses the existing one. Incomplete installation retries at
  the same final path; environments are never moved after creation.
- Heavy models use the existing first-use installation paths. There is no new
  model manager or always-on sidecar service.

## Tests

`npm --prefix desktop test` exercises the token/proxy boundary and real process
cancellation. Python tests cover the service authentication/readiness boundary.
The macOS job mounts the actual DMG, copies the app out, warms download caches,
then removes the dependency environment. A temporary macOS PF rule blocks new external connections while preserving the
runner’s existing control connection. This avoids nesting Seatbelt around
Chromium’s own sandbox. The UI smoke uses no providers or live datasets. The real application rebuilds its private environment from the
cache, imports CSV, runs a local action through its queue, exports, quits and
reopens persisted work. Browser and subprocess cleanup are checked.

Download-cache preparation is a networked build/setup step, separate from tests.
It is not evidence of offline first launch. The automated journey proves the
installed runtime and common UI path, not every optional engine or macOS dialog.
