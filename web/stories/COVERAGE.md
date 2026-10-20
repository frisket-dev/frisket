# Inventory coverage

This is a visual bank, not an exhaustive functional test matrix. The index lists
the actual captured states, source paths, light/dark versions and full-screen
context. Real-screen recipes live in `tests/inventory/`; isolated production
component examples live in `stories/`. All data is synthetic. No models run.

The gallery starts with **small elements**, grouped by purpose: buttons/icon
controls, tabs/segmented controls, dropdown triggers, progress/loading, warnings/
errors/notices, badges/chips, inputs/switches, and labels/help. These are viewport
crops of actual controls in the captured screens, not reconstructed examples.
Repeated labels using the same classes, state and computed styling are collapsed;
this is a practical inventory heuristic, not proof that implementations match.
Each crop links to its screen and includes its selector and scene source paths.
Offscreen or obscured elements are excluded, so this is not a complete code census.

**Larger components and screens** follow separately. Light is the main gallery;
dark versions are optional links rather than duplicate cards. Storybook remains
the interactive companion for the isolated production components.

## Required surface coverage

| Surface | Captured states / approach |
| --- | --- |
| One file | CSV intake, rows, separator menu, columns, destination/disabled name, completed import; paste input; Excel preview and worksheet menu. |
| Multiple files | Intake, copied PDFs, text review, destination, search setup. |
| Split a PDF | Intake, text review, boundary selection, destination. |
| Feed or source | Intake, configured RSS/API/playlist forms; source-create entry from Sources. External feeds are never fetched. |
| Personal settings | Profile, Preferences, Privacy, AI Providers, Diagnostics, Skills; provider-key/local-endpoint add forms. |
| Project settings | General, AI Providers, Secrets, MCP Servers, Plugins, Data Management, Notifications; MCP add form. Access is captured in real Team edition. |
| Organization settings | Real Team owner session: AI Providers, Secrets, API Keys, Usage and Spend, Skills, Models Gateway. No fake local entitlement. |
| PDF Document | Real PDF canvas/text, document list, toolbar, options, alongside chooser. |
| Audio/video Document | Real bundled media, loaded metadata/player, options, video fit. TimedTranscript has additional isolated stories with synthetic segments. |
| PDF Extract | Real saved key/value layout, highlighted fields, scope/options, successful local preview, new-sheet dialog. |
| Audio/video extraction | Actual temporal action forms. There is no audio/video PDF-layout Extract pane in current Frisket. |
| Filter | Populated categorical/numeric filters and active filter. |
| Mentions | Empty/no-entity-column, populated grouped spellings, selected group; populated state explicitly uses API fixtures. |
| Sources / Views / Watches | Sources empty/add, saved-view empty/create/populated, watches empty/create. |
| Notifications / Embeddings | Empty/create and explicit synthetic unread/stale cards. |
| Jobs / Errors / History / Lineage | All four docks, expanded detail area, failed job detail, real import/template history and lineage. Failure jobs are API fixtures. |
| Inspection / search | Row/cell provenance, column history, command palette search. |
| Actions | Template, Classify, Extract fields, NER, Translate, OCR, Transcribe, join, Python, API call, temporal extraction; open model picker and narrow Extract form. |
| Ask / comparison | Ask no-provider form, options, source chooser; OCR/transcription/translation/topic comparison entry tabs. |
| Shared/bespoke controls | Icon/close/chip variants, selects/menus including dialog-hosted portal, segmented control, feedback/progress, structured output editor, delete/cost/extract dialogs, timed transcript. |

Settings are captured through their inner scroll area; full-page alone would
miss lower content. Dialogs/open menus retain full-screen context. Provider
availability is the real unconfigured state, not a fake successful connection.
This inventory is not a Cloud administration/billing UI audit.

## Explicit follow-up states

These remain useful extensions; the entry or leaf captures above **do not claim
to cover them**:

- Multi-worksheet Excel, malformed input, archive questions, bulk cancellation/
  resume, in-flight bulk/split progress and final receipts, append/match choices.
- Feed creation/update/population and source-run health details.
- Populated alongside content, PDF citations, repeated-section drawing, extraction
  diagnostics, image-specific layout, and integrated transcript/citation seeking.
- Date/advanced filter conditions, nonempty watch history, notification delivery,
  embedding search results, complex lineage chains.
- Populated Ask conversation/tool activity and completed model-comparison results.
- Ready-provider setup/submission, real paid estimates, full action preview/result
  combinations. Cost/progress states are shown by actual isolated components.
- Exhaustive keyboard/focus, every breakpoint, every action and browser matrix.

Findings are uncapped in `FINDINGS.md`. The HTML family grids serve as contact
sheets; this is not a screenshot-diff service or a mandatory CI baseline.
