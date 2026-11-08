# Product telemetry

Official installations include the public TelemetryDeck destination. The first-use
screen explains collection and offers a checked toggle; nothing is sent until
Continue. Settings → Personal → Privacy changes the choice later.
`FRISKET_TELEMETRY=off` disables collection for the whole server, including Desktop.
Sensitive projects never forward project events.

Events describe feature use, action/import submissions and observed outcomes,
setup outcomes, exports requested, and Cloud connection outcomes. Values use a
closed vocabulary and coarse size/timing buckets. App/core version accompanies
every event, as do browser family/major version and OS family. Native Desktop
also supplies its shell version, architecture, and OS major/minor version.
Browser OS versions are deliberately omitted: browsers often conceal them.
No raw user agents, machine identifiers, accounts, document contents, prompts,
responses, filenames, column/project names, URLs, or raw errors are sent.

The browser keeps a random identifier and replaces it on the first event of each
UTC month. Logout and preference changes also reset it. Participant counts are
estimates, not account counts, and cannot establish retention across months.
Browser observations can be lost on close/offline; queued imports are admissions,
not completions. No background retry queue is used. Startup failures before the
first-use choice remain in local Desktop diagnostics, not remote telemetry.

## Operation

- `FRISKET_TELEMETRYDECK_NAMESPACE` and `FRISKET_TELEMETRYDECK_APP_ID` override the
  destination together. They are public ingest identifiers, not API credentials.
- Editable development installs default to TelemetryDeck test mode.
  `FRISKET_TELEMETRY_TEST_MODE=true` forces test events; `false` selects production.
- `/api/diagnose` includes `info.product_telemetry`: availability, test mode, and
  the last receiver acceptance or coarse delivery failure in that server process.
  Acceptance means the receiver accepted the HTTP request, not that a dashboard
  query has independently verified it. There is no persistent delivery history.
- Normal Cloud account, billing and job records are operational data, independent
  of the browser's optional product telemetry. Do not join monthly pseudonyms to
  account IDs.

Standard TelemetryDeck dimensions follow its [default parameter reference](https://telemetrydeck.com/docs/ingest/default-parameters/).
Test events are excluded from ordinary production queries; select test mode in
TelemetryDeck to verify them. API querying/account integration is separate from
collection and does not require storing the account password in Frisket.
