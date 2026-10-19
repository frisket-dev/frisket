# UI inventory findings

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

## Rendering anomaly to verify, not yet a confirmed product bug

The [PDF extraction screenshot](extract-populated-local-extraction-preview-light.png)
shows repeated numeric histogram/min/max controls inside the single `size` filter
card. Source review finds one `RangeFacet` per card and one histogram per range,
with no CSS repetition. Check live DOM counts versus screenshot rendering before
filing this as duplicate product markup; the inventory alone cannot establish
the cause.

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
