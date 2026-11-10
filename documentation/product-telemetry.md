# Product telemetry

Official installations include the public TelemetryDeck destination. The first-use
screen explains collection and offers a checked toggle; nothing is sent until
Continue. Settings → Personal → Privacy changes the choice later.
`FRISKET_TELEMETRY=off` disables collection for the whole server, including Desktop.
The aliases `false`, `0`, `no` and `disabled` also disable it.
Upgrading Solo/Team now offers the first-use telemetry choice without operator
configuration; operators can set this environment override before upgrading.
Sensitive projects never forward project events.

Events describe feature use, action/import submissions and observed outcomes,
setup outcomes, exports requested, and Cloud connection outcomes. Values use a
closed vocabulary and coarse size/timing buckets. App/core version accompanies
every event, as do browser family/major version and OS family. Native Desktop
also supplies its shell version, architecture, and OS major/minor version.
Native versions are what the OS reports (Linux kernel major/minor; Windows may
report an older compatibility version). Browser OS versions are deliberately
omitted: browsers often conceal them.
No raw user agents, machine identifiers, accounts, document contents, prompts,
responses, filenames, column/project names, URLs, or raw errors are sent.

The browser keeps a random identifier and replaces it on the first event of each
UTC month. Logout and preference changes also reset it. Participant counts are
estimates, not account counts, and cannot establish retention across months.
Browser observations can be lost on close/offline; queued imports are admissions,
not completions; retry submissions are counted again. Cloud connection success
means device authorization succeeded, not that an account was selected or used.
No background retry queue is used. Startup failures before the
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

## Sample projects and walkthroughs

- `Project.created` has `Frisket.creationKind=sample|blank`; reopening the sample
  does not count as another creation. `Project.opened` records project visits.
- All project events include server-derived `Frisket.projectKind=sample|regular|unknown`.
  Successful sample seeding stores the marker in project metadata, so renaming
  does not change it. Older projects without a marker remain unknown until seeded;
  neither the name nor the slug is used to guess their origin. The marker describes
  origin, not whether someone has since added their own data.
- `Walkthrough.stepViewed` records the built-in guide ID and 1-based step; returning
  to a step or resuming a guide counts as another view. `Walkthrough.completed`
  means reaching Finish, not proof that each exercise succeeded. Custom guide IDs
  are grouped as `other`; no custom names or instruction text are sent.
- Action events include `Frisket.walkthroughState=active|paused|none` and
  `Frisket.walkthrough` (built-in ID, `other`, or `none`). This is UI context at the
  time of the event, not proof the guide caused the action. Use submission events
  when analyzing actions launched during a guide, rather than later job observations.
  The chooser and welcome dialog alone do not count as an active walkthrough.
  Older clients omit these dimensions; absence is unknown, not `none`.

In TelemetryDeck, split project opens and action submissions by `Frisket.projectKind`,
then action submissions by `Frisket.walkthroughState`. Use guide step views and
completion events to explore walkthrough drop-off. Counts remain approximate and
respect the same opt-out and sensitive-project rules as other product telemetry.
