# UI inventory findings

## Responsibility consolidation (2026-10-20; implemented, awaiting visual approval)

All fourteen audit items below are implemented, including non-bug duplication.
Seven parallel implementation lanes shared bounded file ownership; independent
cross-reviews covered every lane. Net handwritten production change versus
`6dc3c869`: **−826 lines**, including all new helpers. This follows the prior
round's −430; no new public PR/merge before maintainer screenshot approval.

Regression tests reproduced project-crossing evidence cache reuse, old saves
clearing new secret drafts, and source-definition drift. Shared source fields,
catalog/selector projections, pull observers, variant editors, evidence caches,
preview transport, notification summaries, provider validation, confirmations,
plugin mounts, clipboard controls and byte formatting replace their old copies.
Feature-specific policies/renderers remain local; no universal manager added.
Reviews caught and repaired stale source errors and clipboard A→B→A feedback.

The integrated run passed 3,763 tests in 474 files; 73 targeted follow-up tests
cover subsequent review repairs and the final formatter cleanup. Types,
Local/Team builds, lint (zero errors, 32 existing warnings) and dependency
boundaries pass. Baseline browser capture passed seven scenarios; final browser
capture passed eight. Independent Codex reviews are clear; Claude's review of
`591b3165` is requested and pending. Final build checking caught
one remaining call to the deleted byte-label alias; it now calls the shared
formatter directly, with no formatting policy change.

The focused review artifact `frisket-ui-responsibility-review-2026-10-20.zip`
combines 33 scenes from this and the previous round, all with current after
captures. Each before image records its actual baseline; no dark-mode inventory
duplication. All 258 local references and archive integrity were checked.
No PR/merge before maintainer screenshot approval.

### Audit rationale and original implementation map

Audited production baseline `6dc3c869` using three independent read-only lanes,
caller/state tracing and a normalized clone search across 687 handwritten
TypeScript files. Clone matches were only leads: repeated composition is not
automatically duplicated responsibility. The findings below are source-inspected;
new failure paths were initially source-only; the status above records the
subsequent implementation and tests. This map does not propose a universal framework.

### Next batch: fix drift while removing its duplicated owner

1. **Source definitions shared by import and editing.**
   `components/importWorkspace/FeedSourceForm.tsx:284` and
   `components/sources/sourceConfig.ts:215` separately serialize the same five
   source kinds, alongside duplicated fields, defaults and validation.
   Numeric CourtListener IDs return early in the import path (`:390`), dropping
   max-entries validation/serialization; editing preserves it. JSON-pointer
   validation also differs. Share source-definition fields and pure config
   validation/serialization. Keep create/edit naming, cadence defaults, redacted
   edits and initial population policy local.
   Verify every source kind, numeric docket limits, bad pointers and redaction
   round trips in feed/source ownership tests.

2. **Comparison catalog and selector authority.**
   `workbench/mediaCompareSession.ts:239` and `TranslateCompareTab.tsx:77` load
   their own catalogs although `state/workspaceResources.ts:691` already owns
   the session catalog. Reuse that owner first. Separately,
   `workbench/useCompareSelectorReadiness.ts:37` refetches selector choices and
   mirrors `canRun`, while `engine-selector/SelectorField.tsx:177` independently
   loads and interprets them (including default/orphaned choices).
   Share the existing selector projection between picker and readiness, keyed
   by project and the complete dependency-aware query. Keep engine eligibility,
   defaults and authored selection with their callers. Verify invalidation,
   setup completion, refresh errors and preservation of edited variants.

3. **Evidence resource scope, then duplicated cache machinery.**
   `workbench/columnEvidenceResource.ts:45` and
   `workbench/evidenceLinkViewerResource.ts:32` duplicate bounded-cache,
   subscription and publication logic. Both caches are module-global and omit
   API/project scope; the column key uses project-local numeric sheet/column/row
   IDs, while the HTTP endpoint explicitly includes project ID
   (`api/projectEvidence.ts:128`). A component remount does not clear this cache.
   First test two projects with identical numeric IDs and fix isolation; then
   share only the small evidence-resource mechanism if it reduces both owners.
   Keep endpoint payloads, empty states and retention bounds distinct. Do not
   introduce an application-wide cache framework. Also test co-mounted consumers
   sharing one request and old requests completing after scope retirement.

4. **Draft-owned save completion, not a universal editor.**
   `components/sources/useSourcesPanelState.ts:57` resets the current form after
   an earlier save, even if it was cancelled and another source opened.
   Similar unconditional clearing exists in project/org secret forms and
   notification channel setup. Use an identity/revision on the existing draft:
   completion may clear only the draft it submitted. Saved Views already has
   this pattern (`state/savedViewsStore.ts:105`).
   Test save A → cancel/edit B → A completes, typing during save, failures and
   overlapping mutations. Retain each domain's validation and permission rules.

5. **Notification summary has one project owner.**
   `components/NotificationsPanel.tsx:94,101,143` refreshes summary directly and
   through its own change listener; `WatchesPanel.tsx:158` independently reads it.
   Share the project summary and make the existing event invalidate it once.
   Keep list pagination and delivery settings separate. Local keyed pending
   state should replace the scalar busy ID that another row's completion clears.
   Test co-mounted consumers, out-of-order summary reads and concurrent row
   mutations. No global mutation manager.

6. **OCR preview quote and execution use the same captured intent.**
   `components/importWorkspace/pdfBatch/usePdfBatchPreview.ts:123` stores an
   estimate/file/page, but confirmation at `pdfBatch/PdfBatchImportFlow.tsx:990`
   rereads current model selection. Selection can change during estimation,
   yielding an old quote with a new engine label/request. Server token validation
   may reject it; unauthorized spending is not established.
   Capture immutable preview inputs with the quote and discard obsolete results,
   using existing import request ownership. Packet OCR already demonstrates
   captured scope/engine/token. Test deferred estimates and engine/draft changes;
   do not invent another general consent framework.

### Following batch: complete, bounded responsibility extractions

7. **Comparison variant editor lifecycle.** OCR/Transcribe configure hooks and
   inline Topic configuration duplicate provisional-add state, focus, dismissal,
   nested Escape and viewport clamping. Share that lifecycle using existing
   popover infrastructure. Keep PDF/player rendering and engine-specific option
   resets outside it. First reconcile the inconsistent blank-variant dismissal
   policy, then test abandoned adds, duplicate/remove, focus and nested dialogs.

8. **Model-pull operation projection.** `ModelPullProgress.tsx:96`,
   `engine-selector/useSelectorChoices.ts:357` and
   `workbench/usePaletteSearch.ts:365` separately track durable pull transitions.
   Share model-pull-specific transport/status handling; reuse `usePoll`, retain
   consumer refresh policies, permissions, cancellation and intentional backoff.
   Do not add another scheduler or a universal job framework.

9. **Source health and post-mutation refresh.**
   `components/sources/useSourcesPanelState.ts:45,106,117` and
   `components/sources/SourceHealthMainView.tsx:77` repeat health loading/publication.
   Drawer data is keyed, but loading/error are shared: late source-A completion
   can alter source-B status. Key status with its request/source and share the
   health resource; locally centralize patch → list/open-health refresh.
   Preserve failed-poll history, pagination and navigation. Test deferred A/B
   responses and successful/failed patches.

10. **Existing-provider validation.** Project/workspace portions of
    `settings/AiProviderSettings.tsx` and `settings/OrganizationSettings.tsx:59`
    duplicate per-provider pending/result/error state. One small scoped hook can
    own it; endpoints and permissions stay separate. Do not combine ordinary
    validation status with `ProviderKeyForm`'s tested-key save receipts.

11. **Remaining native confirmations.** Watch and Saved View deletion still
    duplicate dialog open/Escape/busy handling despite existing `ConfirmDialog`.
    Reuse that component, preserving feature-owned return/next-row focus and
    consequences. Verify forced closure, busy state and cancellation focus.

### Lower priority / only if the extraction stays smaller

12. **Preview-job transport.** Paid media comparison and `previewViewStore`
    duplicate polling/terminal/cancellation handling. Share transport only if
    it cleanly accommodates their existing cadence/retry policies. Preserve
    concurrent quoted comparison jobs versus a single ordinary preview; do not
    merge stores, confirmation or result composition.

13. **Plugin runtime contribution mounting.** Five plugin host files repeat the
    contribution frame + trusted runtime component assembly. A small renderer
    could share this; `pluginHostShell.tsx` already owns admission. Keep typed
    contexts, native fallbacks and per-slot unavailable/hide policies local.
    Avoid a new plugin framework or boolean-heavy host configuration.

14. **Small exact duplicates.** History and CellRunDetails contain identical
    clipboard buttons (including timer/error omissions). Several import/settings
    files redefine byte formatting. Use one clipboard component and existing
    formatting helpers after deciding unit/precision policy. Useful easy deletion,
    but not a substitute for the ownership work above.

### Boundaries and acceptance

- Do not unify media renderers, Watch claims with Saved View tickets, receipt
  authorization with validation status, or source config with name-only forms.
- Claude's possible import busy-on-project-switch issue is not a reproduced app
  bug: `App.tsx:135` remounts the project subtree. Test/document that contract
  rather than build another lifecycle manager around a hypothetical path.
- Start each implementation with the demonstrated mismatch or duplicated
  behavior, migrate all relevant callers, then delete the old owner. Prefer an
  existing owner over a new abstraction. No bridge left merely to ease migration.
- Use focused behavioral tests (deferred requests for ownership problems) and
  changed-surface screenshots; broader verification after integration. Count new
  files in net handwritten production LOC; no speculative savings estimate.
- The audit itself was read-only; implementation status is recorded above.
  The preceding round is independently reviewed and Claude-cleared; maintainer
  visual approval still precedes public PR/merge.

## Comparison, import, list and field consolidation (implemented; visual review)

Baseline: `bc61d79e`. Four bounded lanes: shared comparison markup; repeated
import lifecycle operations; Watches/Notifications editable-list presentation;
generated action fields and filters. Preserve domain behavior, consent and
cancellation. Reuse real implementations rather than introduce a universal
wizard, list controller or form DSL. Count new files in net production LOC.
Integrate with focused tests, browser captures of changed surfaces, independent
correctness review and Claude visual review. Deliver a focused before/after ZIP;
no public PR or merge before maintainer visual approval.

Implemented shared comparison toolbar/chips/result frames; import-owned request
replacement and shared table intake; shared list save/cancel controls and local
notification mutation bookkeeping; common scalar/member facet presentation and
parameter/NER field rendering. Domain controllers and consent remain separate.
Packet polling now uses the existing overlap/visibility-aware poller.

The new browser coverage reproduced the previously suspected repeated range
controls: range and advanced-condition siblings could both have the key
`3:integer:null`. Namespaced keys fix the orphaned DOM; a regression fails before
the fix and passes afterward. No range arithmetic changed.

This round removes **430 handwritten production lines**, counting all new
components/hooks. CSS is +6; the reduction is in TypeScript/JSX this time.
Focused tests and independent code review cover all four lanes. The integrated
frontend suite passes all 3,649 tests in 459 files; types, dependency boundaries,
Local/Team builds and lint pass (39 existing warnings). Six browser scenarios
pass, including populated forms/results and narrow comparison toolbar ordering.
Baseline captures used the frozen pre-change source; their numeric interaction
failed on the real duplicate-key bug. The same strict interaction passes after
the fix. Busy icon animation belongs to the shared icon button, not notifications.
Claude cleared candidate `6dc3c869`; maintainer visual approval remains before
PR/merge.

## Card, dialog and compound-editor round (implemented)

- Normalize card/row contents across Sources, Watches and Notifications: titles,
  descriptions, metadata, counts and status badges, not only their outer frame.
- Replace remaining custom export, embedding, telemetry and AI-mode modal
  backdrops with the shared dialog lifecycle; retain all consent/busy safeguards.
- Consolidate compound-editor row controls and label/description presentation,
  preserving domain-specific state rather than building a generic form framework.
- Delete duplicate CSS throughout those changes and co-locate remaining
  specialized rules. Moving lines alone is not counted as reduction.

The next maintainer ZIP includes **changed surfaces only**, spanning the previous
structural round and this round. It is not another full component inventory.
No public PR until visual approval.

All four items are implemented. Dialogs share Modal; imports and embedding
creation share StageStepper. Compound editors share presentation, not a new
form/controller framework. Card metadata and status presentation now use the
same primitives across Sources, Watches and Notifications.

Reviews caught label activation removing a Join selection, nested dialog Escape
closing its parent, and narrow repeated-section inputs hiding page numbers.
These are repaired and covered by component/browser regressions. Required
telemetry consent and AI-mode confirmation remain intact. CourtListener checkbox
spacing, selected canonical-value styling and dialog accessible names were also
corrected. Existing export cancellation behavior is unchanged.

Production delta versus `94d0f2cc`: +681 / −1,508, **net −827**. Entire
normalization campaign versus `ebe6301d`: **net −3,485**. These totals include
new stylesheets and components; moving CSS alone is not counted as savings.
Targeted component tests, application/inventory types and lint pass. The broad
browser pass had nine passing scenarios and one fixture failure (a read-only POST
was incorrectly denied); the corrected cards, dialogs and populated editors
then passed all four final scenarios. Captures use offline synthetic/local data.
Independent Codex and Claude reviews cleared the final production candidate
`a6d0b6fa`; maintainer visual approval remains pending before any public PR.

## Active expanded consolidation scope (maintainer approved)

1. Split ImportCsv by responsibility: feed setup, file intake, follow-up prompts,
   bulk-plan presentation and workspace orchestration; no compatibility facade.
2. Consolidate panel header / scrolling body / fixed footer presentation.
3. Remove page-specific overrides of shared controls, normalizing incidental styles.
4. Reuse editable-row, card, summary and toolbar presentation across real callers.
5. Consolidate menu/popover positioning and dismissal on existing infrastructure;
   fix nested Escape dismissal without merging unrelated selection controllers.
6. Delete verified obsolete CSS and measure net handwritten production LOC.

Proceed aggressively, preserving features and domain logic rather than incidental
styling. Review each bounded round with targeted tests, before/after captures and
secondary review. Fold in maintainer visual feedback. No public PR until all
revamp cycles and maintainer visual approval are complete. The first split is an
ownership prerequisite, not itself claimed as a LOC reduction.

### Structural round status

Implemented: named import-workspace modules (no ImportCsv facade), shared
PanelFrame/Body/Footer for imports, drawers and receipts, AnchoredMenu for
shared placement/dismissal, and shared form/list/card presentation in plugins,
cleanup actions, embeddings and evidence. Dead import CSS and page-specific
control skins were removed. Domain controllers remain separate.

Review found keyboard entry into portaled menus and hover-open Saved Views
Escape problems; both now have browser regressions. The broad capture also
caught an inline output menu inheriting fixed positioning: sizing and placement
must remain separate. Small-control screenshots caught squeezed Substitute
source and Replace match controls; those are included in the final repair pass.

Production `web/src` delta for this structural round: 541 lines added, 1,575
deleted (net −1,034), excluding tests, stories and generated artifacts. The
preceding import ownership split was net +1. Across the full normalization
campaign, production code is net −2,658 versus `ebe6301d`.

Visual approval is pending. This inventory is a running audit, so observations
below describe their original baseline unless marked resolved; they are not all
remaining implementation work.

Working audit, not a redesign proposal. Record all substantive findings; no quota or cap. Code observations below are preliminary until accompanied by rendered evidence. Screenshots will be linked from the generated inventory. Group repeated symptoms under their shared cause, and retain intentional behavior differences.

## First consolidation pass

The baseline findings below prompted shared `TextInput`/`TextArea` controls,
one segmented-control stylesheet (including PDF preview/method choices),
shared import/dock progress and additional IconButton/WindowCloseButton use.
Settings feedback and table-preview notices now use shared panel feedback.
Page layout stays local; size, width and status differences belong to props.

This is not a complete replacement of every custom element. Remaining useful
targets include icon buttons with loading/destructive states, provider-validation
feedback, label/help spacing, compound unit inputs and reader navigation.
Inline title editors, code editors and template composers need their editing
behavior preserved, not merely a native-input tag swap. The Escape propagation
defect and repeated-histogram observation below are separate from this pass.

## Consolidation scope and structural observations

### Normalization pass (maintainer-approved)

Normalize incidental differences instead of encoding them as more variants.
Shared Button/TabButton, FormField/SearchInput, Modal/ConfirmDialog and existing
feedback/segmented controls now own presentation across actions, settings,
imports and panels. Source-health content and comparison-discard dialogs share
implementations. Import steppers no longer have three separate skins. Schema
field layout no longer requires an action-panel ancestor. Layout placement,
specialized editor behavior, keyboard handling and confirmation safeguards stay
with their owners. Inspect narrow layouts and nested dialogs in the screenshots;
different spacing/colors alone are not regressions to restore.

### Maintainer visual review: distinctions to keep

- Footer placement is layout, not a button variant: action cost/Preview/Run stays
  at the drawer bottom and fills its width. Browser geometry assertions cover it.
- Fields may be stacked or inline; PDF options use one aligned key/value row.
- Text-style disclosure buttons inherit their surrounding size/color. Provenance
  is metadata, not a full-size call to action.
- Tab groups retain regional colors through a shared tone parameter.
- Select triggers share padding/borders/chevrons; rich model choice content does
  not require a different outer control skin.

### Larger-component rounds (implemented; maintainer visual approval pending)

1. **Import framing.** There is already one outer dialog/header in ImportCsv,
   plus shared ImportStageStepper/ImportStepFooter/destination controls. Consolidate
   internal step framing, summary/help placement, progress and hardcoded skins.
   Remove flow-specific overrides in ImportWorkspaceShared.css rather than add
   another wrapper or a generic wizard/state machine. Keep CSV parsing, PDF
   splitting and file-batch lifecycles separate.
2. **Document/Extract browser chrome.** Share the search/list/header/empty-state
   framing around the existing common reader. Preserve extraction's editing state.
3. **Panels and settings forms.** Common header/action strip, list/detail frame,
   field spacing and feedback; retain separate fetching and domain controllers.
4. **Compound controls.** Continue consolidating dropdown trigger/menu styling
   and small field-group layouts. Searchable lists, multi-select and model browsing
   need different behavior, not automatically different visual treatments.

### Import framing round

Table and multiple-file steps now use ImportStep/ImportStepBody with the existing
ImportStepFooter; split-PDF's destination joins the same structure. One workspace
header owns sizing and stage placement instead of three descendant overrides.
Multiple-file progress uses ActivityProgressCard. Shared destination choices no
longer have table-only padding overrides; table/batch colors use application tokens.
Specialized column previews, split boundaries and text-check sidebars remain
local layouts, not a generic wizard. Domain state and callbacks are unchanged.

The follow-up review also caught two non-select buttons wearing row-height-select:
comparison Source/Wrap toggles now use Button with a shared pressed state. Reader
option width is owned by anchored positioning (including viewport bounds), not a
CSS minimum that disagrees with the positioning calculation.

### Application styles are coupled to startup

`src/entries/mount.tsx` loads the shared fonts and stylesheet but also installs global error listeners and mounts edition providers. The catalog must import the style assets without importing that entry. A small side-effect-free style entry shared by both could reduce drift if font/style imports expand; this is not a reason to redesign application bootstrap now.

### Component names do not describe their actual breadth

`PanelSelect` and `PanelPrimitives` now serve more than action panels. Audit use sites before inventing another settings/import-specific control. `WindowCloseButton` already wraps `IconButton`: these are not duplicate implementations merely because both exist.

### Test fixtures mix data construction with test-runtime installation

`tests/support/selectorChoicesFixture.ts` includes useful response construction alongside Vitest spies and DOM polyfills. Stories should reuse pure data where possible, not depend on test-runner globals. If reuse grows, separate the pure fixture builder from installation rather than adding a browser-specific second copy of the same response schema.

### Default test setup is unsuitable for a visual inventory

`tests/gallery.ts` requires named seeded projects and performs a live model run. `playwright/playwright.config.ts` also depends on paid-capable seeding; `playwright.local-stack.ts` defaults to a persistent base workspace. Use a fresh isolated fixture stack for inventory. Do not generalize one-off inventory needs into another environment manager.

## Visual and interaction findings

The links below are relative to the generated inventory (the capture command copies this document there). Source paths are relative to `web/`. A screenshot proves appearance, not a complete accessibility or behavior audit.

### Resolved interaction defect: nested Escape dismissal

In the baseline CSV row-preview step, Escape closed Separator and also opened
the parent import's discard confirmation. Shared native-popover dismissal now
closes only the nearest layer and prevents the enclosing dialog's cancellation.
Browser assertions cover import separators, nested document options, action
help/title menus and hover-open Saved Views. See [separator menu](import-workflow-table-separator-menu-light.png).

### Rendered comparisons worth acting on

| Area / example | Observation and possible next step | Preserve |
| --- | --- | --- |
| [Close controls](shared-controls-close-controls-light.png) | Shared IconButton/WindowCloseButton coexist with ActivityProgress's hand-built dismiss button. Check remaining `icon-btn` call sites against these existing controls. | Inline, compact and window target sizes are intentional variants. |
| [Shared feedback](shared-controls-feedback-light.png) | Empty/loading/error/progress already have common primitives. Inventory the bespoke settings/panel equivalents before adding another status treatment. | Error detail, retry, progress and empty state are different meanings. |
| [Delete dialog](dialogs-delete-rows-light.png), [cost gate](dialogs-cost-gate-light.png), [sheet dialog](dialogs-extract-sheet-light.png) | Similar framing uses native dialog in some cases and a div backdrop/form in others. Focus, Escape and layering should have one tested implementation where semantics agree. | Cost confirmation and destructive confirmation retain their distinct text/actions. |
| [Extract model picker](actions-extract-model-picker-light.png), [source select](shared-controls-select-open-light.png) | A searchable, grouped setup dialog is not a simple select. Share trigger typography, indicator and focus treatment; do not combine their controllers. | Provider setup/details/search versus immediate single selection. |
| [Structured output fields](shared-controls-structured-fields-light.png) | Field names, descriptions, type dropdowns, required markers and remove controls form a compact editor. Compare its spacing and state vocabulary with Extract's field rows. | Structured schema editing is not the PDF layout editor. |
| [Watch composer](side-panels-watch-create-light.png) | A bordered card inside a panel contains yet another inset notice. Compare form grouping, notice styling and button spacing against settings/source creation. | Watch-specific destination warnings and creation behavior. |
| [Mentions](side-panels-mentions-grouped-forms-light.png) | Group labels, count chips and expanded spellings have several independently styled text treatments. Compare chips/disclosures with source/view/job rows. | Mention grouping and counts remain domain-specific. |
| [Errors detail](bottom-dock-errors-failed-details-light.png) | A status table and detail pane are an existing useful composition. Several other list/detail screens independently recreate that structure. | Dock sizing, row selection and error-specific remediation. |
| [PDF layout preview](extract-populated-local-extraction-preview-light.png) | Document and Extract reuse the reader, but separately assemble list/search, field controls and options. Favor extracting genuinely identical chrome, not a new universal editor. | Drawing, citations and editable extraction fields have different interactions. |
| [Import reader](import-workflow-table-check-rows-light.png) | The options row mixes selects, checkboxes and a numeric stepper. Compare sizes and labels together, not just dropdown arrows. | Native select/input semantics and CSV-specific options. |
| [MCP form](settings-project-mcp-server-form-light.png), [provider form](settings-personal-provider-key-form-light.png) | Settings sections use different form/label/button vocabularies for similar add/edit tasks. Audit a small common field/notice layout before any schema-driven rewrite. | Secret handling, connection testing and provider-specific validation. |

## Further code-grounded opportunities

These are candidates to investigate, not assertions that every difference is a defect. They are deliberately not capped or reduced to a top-five list.

1. Done: `ImportCsv.tsx` was removed. Workspace routing, file intake, feed configuration and follow-up presentation now have named modules in `importWorkspace/`.
2. Done: import flows share `ImportStageStepper`, destination controls and the same PanelBody/PanelFooter used by drawers and receipts.
3. PDF batch and PDF packet sample switchers use different markup for related navigation. Share their small controls where appropriate, not their different review algorithms.
4. Multiple-file import assembles its own progress cards despite `ActivityProgress`. Compare cancellation/retention behavior before replacing it.
5. Import reader controls wrap selects in implicit labels; accessible names include option text. Explicit associations could simplify the names without changing dropdown semantics.
6. Destination summaries vary by flow. Common outer layout may help, but domain summaries should not become a large conditional component.
7. Sources repeats health summary/run/job markup between drawer and main-view presentations. Extracting that shared content looks promising.
8. Sources, Watches and Notifications each have bespoke list/header/body/status classes. Reuse framing if screenshots support it; do not merge their network controllers.
9. Settings `InlineStatus`, `settings-error` and `settings-inline-error` overlap with panel status primitives. Pick a consistent meaning-to-presentation mapping.
10. Skills owns a split list/editor, bespoke refresh control and separate error treatment. Reuse established controls without erasing its editing workflow.
11. Embedding creation uses a div backdrop, unlike several native dialogs. Include it in the dialog/focus audit.
12. Discover's comments/test helper still describe an older tab set; Views needs its own equivalent navigation helper. Update discovery vocabulary, not just screenshot selectors.
13. Internal `Facets` and `pdfBatch` terminology no longer matches Filter and Multiple files. Rename only when it helps ownership/discovery; this is less urgent than actual duplication.
14. Core settings plus capability/edition contributions are legitimate composition. A component inventory reading only the core registry silently misses real pages; avoid creating a second manual registry.
15. DocumentReader's custom zoom/page/options controls sit alongside shared buttons in the alongside pane. Compare sizing, disabled and focus states before replacing them.
16. Document options and extraction options have related row styling but different purposes. Small row/popover primitives may be useful; one combined options controller is unlikely to be.
17. Extraction fields combine editable labels, examples, locate/delete and validation. They are a good stress test for a shared form-row vocabulary, not justification for a generic form DSL.
18. Full-page screenshots do not capture internally scrolling settings panes or collapsed dock details. The recipes explicitly scroll settings and expand the dock; preserve that when extending the bank.
19. Old E2E examples contain obsolete model-picker selectors and typed-confirmation assumptions. Reuse data shapes only after checking current code; do not mistake those tests for current product requirements.
20. `StructuredFieldsEditor` depends on an `.action-panel` ancestor for its actual row layout. Without it the real component becomes a vertical stack, despite importing all production CSS. The story now supplies the real container; the hidden CSS dependency is a concrete obstacle to reuse elsewhere. Move ownership with the component if it is reused, rather than copying those selectors.
21. `GeneratedActionForm` derives “Checking engine availability…” from a missing current choice, not a loading flag. The unconfigured-model capture can therefore look busy even when the picker has loaded. Check this distinction before standardizing loading/unavailable states.

## Repeated range controls (confirmed and fixed)

The [PDF extraction screenshot](extract-populated-local-extraction-preview-light.png)
showed repeated numeric histogram/min/max controls inside one filter card.
The subsequent browser interaction test confirmed real duplicate DOM, not a
screenshot artifact. Range and advanced-condition sibling keys collided when
the preview hash and condition were null. Their keys now have distinct prefixes;
component regression coverage asserts one range editor across refresh/filter
transitions. The numeric browser selector remains strict.

## Suggested first consolidation passes (after maintainer review)

1. Fix nested dismissal and noisy accessible labels with focused interaction tests.
2. Replace remaining equivalent close/refresh/remove controls with existing primitives.
3. Align notices, errors and form rows across settings/import/panels, preserving meaning.
4. Extract Sources' repeated health content and repeated Document/Extract list chrome where behavior truly matches.

The inventory and normalization remain on the working branch; no PR until the
maintainer finishes reviewing the revamp cycles.

## Additional code findings from coverage review

- `settings/SettingsSections.tsx` concentrates many unrelated settings sections and async behaviors in one large module. Investigate ownership and fixture coupling, not arbitrary file-size targets or a one-component-per-file rule.
- `components/ActivityProgress.tsx` and `workbench/extract/ExtractView.tsx` still assemble some `icon-btn` controls directly despite the existing shared `IconButton`. Compare focus, active, disabled, glyph, and target sizing before replacing.
- Extract reuses `useDocumentView` and `DocumentReader` but owns additional document-search/list markup. Compare that chrome against Document view for drift rather than creating another reader abstraction.
- Extract assembles help/options placement and dismissal; DocumentReader uses `useOptionsPopover` plus a render prop. Check whether a small common overlay primitive would help, retaining differing interaction requirements.
- `importWorkspace/pdfBatch` now implements the broader Multiple files workflow. Internal Filter naming still includes `Facets`. Both make discovery harder; naming cleanup is distinct from a visual defect.
- Extract's main view is PDF/image-only (`core/selectors/workView.ts`, `workbench/extract/ExtractView.tsx`). Audio/video temporal extraction is an action form, not another Extract pane. Record unavailable UI and the separate supported workflow, without inventing missing features for this audit.

## Document/Extract consolidation

`DocumentList` now owns the existing windowed list markup, search, count,
empty/error states and pagination. Hosts keep selection effects and per-document
details (reading priority versus extraction outcomes); fetching still belongs
to `useDocumentView`. Reader/header reuse was already in place, so no new editor
or controller abstraction was added. Direct tests cover windowing and callbacks.

Visual review also exposed a generic menu max-width overriding the positioned
PDF options width. `MenuPop sizing="anchored"` lets measured placement own width;
the browser inventory now asserts it. Import headers retain centered steppers,
and unequal-height progress cards no longer stretch into empty bands.

## Panel/settings framing

Settings sections already share their heading frame and feedback primitives.
The remaining repeated layout rules now use `content-stack`, `control-row`,
`panel-list` and `panel-card` across settings, Sources, Watches and Notifications.
These style semantic markup directly, without a new layout/controller framework.
Notification fields reuse `FormField`; Skills uses shared empty/error/refresh
controls. Domain severity, selection, status and gutter ownership remain intact.

List/detail isn't one behavior: Skills edits a selected text document; the dock
inspects jobs; Sources exposes health and runs. Their fetching and selection
remain separate. Future consolidation should reuse presentation where useful,
not impose one master/detail state machine on those workflows.

## Compound-control/dropdown presentation

Single selects, multi-column selection and the editable output-name picker now
use `MenuPop` and shared menu option/empty/selection styles. Trigger appearance
and chevrons were already shared. Native select keyboard behavior, multi-select
checkboxes, editable names and model setup remain distinct controllers.
The model picker is a searchable setup/detail dialog, not an ordinary menu;
its shared trigger is the correct boundary, not collapsing that dialog into a select.

Review follow-ups: wide inline forms use a bounded label track; feed-import
prompts no longer reference the retired Sources layout, and their labels and
controls use field groups. Inventory now crops an open import dialog instead
of the obscured Sources panel behind it.

## Import consistency follow-up

User review caught three independently styled file drop zones and entry screens
without the shared footer. They now use `ImportDropArea`, `ImportStepBody` and
`ImportStepFooter`; feed and URL submission also sit in the footer. Footer Cancel
uses the same close guards as the header. Multiple files and Split PDF still
process selection immediately; progress screens retain their existing controls.
Table destination/summary width caps and the extra file-source toggle skin are
removed. Browser checks cover drop-area size, footer anchoring and destination
width; the comparison now includes Excel destination and URL entry.

## Frontend ownership refactor (implemented; awaiting visual signoff)

Baseline: `845f9a3e70855818b52eb3c935357eb58b2bbeac`, after the approved
normalization screenshots. This phase changes ownership, not product behavior.

- App owns routes; workspace region components own their rendering. Existing
  stores and subscription boundaries remain in use.
- Workspace navigation, grid operations, saved views, action execution, view
  commands, contributions and replay review have cohesive controller owners.
  Stable projections must retain unrelated-region render isolation.
- Settings sections are grouped by responsibility, with no compatibility barrel.
  Organization/workspace model-pull history now shares its actual implementation.
- Row inspection separates values, structured JSON, citations and run details;
  Evidence separates fetching/composition from document, media and text readers.
- Sources separates configuration state, editing, listing and health views.
  Shared health content serves both drawer and main view.
- Global CSS retains foundations; component/feature styles load from their
  owners. Shared visual primitives retain shared styles. Lazy imports and
  cross-owner selectors need browser verification, not just stylesheet diffs.

Acceptance: targeted interaction tests and independent review per lane, followed
by integrated types/lint/tests, representative browser workflows, render-count
checks and focused before/after screenshots. All new files count in LOC totals;
moving code is not a reduction. Final visual approval precedes PR and merge.

Candidate: `a1604fd6c2bf991fc1e4abc9111b09bf708d73d6`. All 456 frontend test
files / 3,620 tests pass; types, ESLint and dependency/boundary checks pass
(existing hook warnings remain). Local/Team builds and Storybook build pass.
All 20 inventory scenarios pass across the initial run and the repaired dialog
fixture rerun; four representative scenarios also pass against built production
assets. The render-isolation script passes twice with unchanged thresholds.
It now waits for real catalog/detail responses before measuring subsequent
interactions, rather than misattributing late mount work to theme changes.

Independent reviews covered every extracted feature, workspace composition and
CSS ownership. They caught shared header cascade and cancelled-status ownership;
both were repaired. Stable Ask adapters, export-owned subscriptions and guarded
scope resets remove unnecessary rerenders. Existing React Doctor errors in
WalkthroughProvider, PdfPacketImportFlow and useJoinSheets remain separate work;
newly introduced refresh/helper findings were fixed, not suppressed.

Honest size accounting: this ownership round is **+293 production lines**,
including all new owners and imports; CSS itself is **−148 lines**. The complete
normalization campaign remains **−3,192 production lines** against `ebe6301d`.
App is now 302 lines, workspace composition 1,201 and global CSS 235; the moved
implementations still exist under their responsible features.

Follow-up observations, outside this ownership change: cell-audit and some
manually refreshed source-health requests lack stale-response protection.
The bundled Geo view still receives its stylesheet through the plugin host,
because the existing plugin loader has no separate CSS-asset interface; this
does not justify adding an asset framework to this refactor.

### Review corrections and import destination reuse

Secondary review caught a genuine import-rail cascade regression: its obsolete
five-column layout overrode the dialog's vertical layout. The rail now owns
desktop/narrow geometry in one stylesheet. Redundant button/menu/field paint
was removed rather than restoring a fragile global import order. Browser tests
render representative controls with feature/primitive CSS loaded in both orders.

Missing ribbon tabs in some captures were premature screenshots: recorded
catalog requests completed successfully after the capture. Inventory now waits
for the application's actual catalog response and rendered tabs, and for settled
comparison-engine controls. No production loading behavior was changed.

Excel/table and multiple-file imports now share the actual destination picker,
not merely its cards: identical new/existing labels, fields and disabled states,
with destination state and matching logic retained by their flows. The required
asterisk remains a toggle, now unboxed grey/red; poll history is a plain disclosure.
Text-button hover no longer introduces a filled box, while selected extraction
preview rows retain their intentional background.

This follow-up removes another 91 production lines: ownership refactor net +202,
full normalization campaign net −3,283. Targeted tests cover destination defaults,
preferred sheets, disabled controls, required-toggle keyboard behavior and sources.
Production-browser checks cover imports, actions, sources and comparisons; all
seven checks pass, including both CSS-order regressions. Six further production
checks pass for documents, dialogs, transcripts and shared-control stories.
The earlier development capture was terminated after 12 passing scenarios;
the remaining coverage was completed with the production runs, not waived.
Final visual approval still precedes PR/merge.
