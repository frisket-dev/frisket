# Public release publishing

Run `release-public-artifacts` from `main`, specifying the source commit in
the `ref` input. The workflow builds and tests the artifacts before a separate
publishing job uploads them to a draft and publishes it. Publication verifies
that GitHub made the release immutable. Use a new version for corrections.

One-time repository setup:

- Enable **Settings → General → Releases → Enable release immutability**.
  This affects future releases; it does not lock existing releases retroactively.
- Create the `release-publishing` environment, allowing branch `main` and tags
  `v*`. Set environment variable `RELEASE_APP_ID` and environment secret
  `RELEASE_APP_PRIVATE_KEY` for the installed publishing GitHub App.
- Give that App repository Contents write permission and permission to bypass
  the release-tag creation rule. Keep the tag rule enabled.

The publish job mints a short-lived token for this repository only, requests
only Contents write permission, and revokes it when the job ends. Build jobs
do not receive the App credentials, and the publisher does not check out or
execute source build scripts. There is no PAT or default-token fallback.

Desktop publishing has its own workflow and signing environment.
