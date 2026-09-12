# Engine and model selection

Status: technical specification under review for #54. This replaces the separate
engine, AI-model and embedding picker implementations with one reusable selector.
The specification covers backend eligibility/setup as well as presentation.

## Product contract

The picker presents **provider → choice → detail**. Local, Models server and API
providers group the existing executable choices; this change does not let users
choose another execution location for the same engine. Engine/model values keep
their existing action contracts. A resolved execution target is descriptive
metadata, never a new field silently added to an action request.

The details explain capabilities, processing destination, available pricing and
applicable setup. Unknown facts are omitted; unknown prices are not free. Model
descriptions are curated data with their existing backend owners. Never derive
capabilities, privacy or permissions by parsing a display label.

Selecting an authorized choice that needs setup is allowed, but execution stays
blocked until its required setup is satisfied. An explicitly declared automatic
first-use download may prepare the model as part of Preview/Run. Unsupported or
policy-forbidden choices are not newly authorable; an unavailable saved choice
remains visible for correction. No automatic replacement or remote fallback.

Opus-MT displays **“Downloads the required language model on first use.”**
Source/target language controls remain in the action form. A supported missing
pair downloads on Preview/Run, with visible preparation progress, then translates.
There is no preference checkbox or mandatory manual download step. Opening the
picker, hovering, selecting an engine or changing a language never downloads
weights. Missing runtime or unsupported pairs remain actual blockers.

## Ownership

Execution/credential/model-artifact owners remain authoritative. The new read
contract projects those facts for the selected project and usage context; it is
not a second catalog database. The browser formats facts and invokes typed setup
operations. Server admission independently enforces existing permissions,
capability, network, consent and cost rules.

One controlled visual component owns navigation and presentation. A controller
owns scoped loading/setup and asynchronous request lifetime. Form-specific
adapters own conversion between a selected choice and existing authored values.
No component independently rediscovers provider keys or invents target routing.

The complete API and setup contracts are finalized at the technical review gate
before implementation. The sections below fix the shared visual and migration
contracts so that backend and frontend work can be assigned without overlap.

## Shared frontend component

Place the shared visual component, detail rendering, public types and scoped CSS
under `web/src/components/engine-selector/`. Prefer a few cohesive files over a
separate component for every label. Setup controllers live outside the visual
component and provide a typed detail-footer slot; Settings reuses the same setup
controls where applicable.

The visual component receives groups, choices, the current authored choice ID,
a label, a recent-choice namespace, an `onSelect` callback and a setup/footer
render slot. It does not fetch catalogs, save keys, resolve targets, download
models or write action params. Mixed engine/model callers convert the selection
to their existing engine/model fields atomically. Embedding callers preserve
their provider/model pair and modality restrictions.

Keep committed selection, previewed choice and pinned setup detail distinct.
Opening focuses search and reveals the committed selection. Hover previews a
group/choice after 120 ms and protects diagonal pointer travel into the detail
pane; it never moves keyboard focus. Entering a setup form pins the detail so
hover cannot replace edited inputs. Explicit navigation can leave that detail.

Ready/authorable click selects and closes. Setup-needed/authorable click selects,
keeps the dialog open and focuses its setup. A hard-unavailable click only shows
the reason. Saving a key or server configuration refreshes readiness without
closing or changing the committed choice. Setup completion cannot overwrite a
newer project or selection.

Search matches label, summary and group across offered choices; results have
group tags and retain the detail pane. Hide the provider column with one group
or at most six choices. Above twelve choices in one provider, show its local
filter. Remember at most three recent IDs per workspace/action/browser context;
filter them against the current authoritative list. Default is a badge on the
actual default choice, not another group.

### Layout and accessibility

- Use one native modal dialog styled as an anchored popover on wide screens.
  The workbench beneath it is inert. Anchor to the whole field's right edge,
  opening left, width 760 px, maximum height 420 px; columns 160 / 230 / remainder.
  Model list and detail scroll independently. Use the existing positioning
  primitive and theme tokens, with scoped styles.
- Below approximately 1000 px or when the anchored view cannot fit, center a
  92%-viewport-width dialog and use provider tabs. Below 600 px, tapping a choice
  opens a detail sheet with Back and explicit Select/setup controls. Preserve
  list position and sensible focus on returning.
- Model/provider lists contain ordinary navigable buttons, with real setup
  inputs in the separate detail region. Do not nest forms inside listbox options.
  Up/Down navigate within a list; Left/Right move between provider and choice
  lists. Enter selects, Tab reaches details/setup, Escape closes and returns
  focus. Do not intercept text-field editing keys.
- List-valued facts expand from a count into chips. Status uses text as well as
  a colored dot, with concise progress announcements. Honor reduced motion.
  External model links use the existing web/Desktop external-link policy.

## Complete migration map

| Existing surface | Migration |
| --- | --- |
| `GeneratedActionForm` engine and model fields | Shared selector/controller, existing draft and run contract. Replace separate availability disclosure with shared status. |
| `EngineModelChoice` for classify/NER/translate | Keep atomic authored engine/model mapping; replace visual menu. |
| Copilot model selector | Same component with Copilot context; no row estimate required. |
| Embedding model selector/create dialog | Same component with embedding context; retain custom-ID/modality/dimension behavior. |
| OCR/ASR compare add and configure menus | Same component; keep each variant's options, duplicate behavior and existing execution contract. |
| Translate/topic compare menus | Same component; retain their existing eligibility and billing restrictions. |
| NER model download | Move engine-level setup from the inline artifact control into shared detail. |
| `TranslatePairPicker` | Keep source/target controls, replace manual install/onInstalled flow with automatic first-use disclosure and run progress. |
| Settings provider/server/download controls | Reuse setup controls and authoritative operation state; retain administration scope. |
| Jobs/activity and selected-field status | Display durable explicit model pulls and run-local preparation using their actual owners; no duplicate jobs. |

Delete replaced `EnginePicker`, `ModelPicker`, embedding picker UI and obsolete
picker CSS once their callers have migrated. Preserve useful source adapters and
result badges only where they still have callers. No permanent parallel picker.

## Proportionality

The realistic failures are an incorrect credential/project scope, confusing
missing weights with missing runtime, setup completing after context changes,
an unreachable or incompatible server, and incomplete migration. Use a small
number of catalog/setup/public-run tests plus representative component/browser
journeys. No source-shape analyzer, generic installer/DAG, hardware certification,
new pricing engine or exhaustive live-model test matrix.

Managed model-service Start/Stop and “Always launch on startup” remain #46.
This delivery includes server attachment/setup, not a container/process manager.
Changing Desktop's bundled weight policy is separate from making the picker and
first-use setup work.
