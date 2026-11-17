# Pull requests run a fast tier; the merge queue runs everything

A pull request runs the gates and the tests of the areas it changes; the merge queue runs the complete suite on the commit that lands; macOS and Windows installed-app and portability proofs run nightly on `main`, and on a pull request or queue entry only when their own inputs change. A release requires a green nightly proof of its exact commit (`.github/workflows/release-public-artifacts.yml`, `desktop-release.yml`).

**Considered:** everything on every pull request. Slower feedback and a higher CI bill on every push.

**Consequences:** a green pull request is not a full test. A platform break can land and is caught by the next nightly, which opens an issue, but cannot ship; the `ci:full` label runs everything on a pull request.
