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

## Project-scoped choice contract

Add side-effect-free `POST /api/projects/{pid}/selector-choices`, registered as
`tenant.selector_choices.post` in the viewer and browser-client endpoint catalogs.
Typed DTOs belong in `contracts/http/selector_choices.py`, admission in
`server/routes/selector_choices.py`, projection in `server/services/selector_choices.py`.
Publish OpenAPI and TypeScript through the existing generators.

Request schema `frisket.selector_choices_query.v1` has a discriminated subject:

- `action`: `action_id`, `field`, sparse current draft `params`.
- `copilot`: optional current `model`.
- `embedding`: current `provider`/`model`, optional `modality` and
  `source_column_type`.

An action field must be a declared engine/model field. Only capability-affecting
options are needed; missing source/output fields do not invalidate loading.
Reject unknown actions/fields and authored target overrides. Do not echo arbitrary
draft params. Response schema `frisket.selector_choices.v1` contains normalized
subject, project ID, `depends_on` option fields, current/default choice IDs,
groups and an optional blocked `orphaned_current`.

Groups have stable IDs, kind (`local`, `server`, `provider`), label, derived status
and choices. Choice fields:

- UI-only `choice_id`, label, short summary, description, optional model-card URL,
  ordered typed facts (text, list or numeric rate).
- `authored_selection`: discriminated `engine {engine}`, `model {model}`,
  `engine_model {engine, model: string|null}`, or `embedding {provider, model}`.
  Preserve opaque IDs including nested OpenRouter paths; this is the only mapping
  to caller values.
- Resolved target and processing destination as descriptive facts. Unknown
  destination produces neutral copy, never an invented local/privacy claim.
- `status: ready|needs_setup|working|unavailable`, `can_author`, `can_run`, optional
  typed blocker, setup descriptor and active operation projection.
- Default/current flags from the usage context, not list order.

`can_run` is selector preflight, not a promise that inputs, quotas or consent pass
admission. Only the active resolved target contributes readiness; another ready
target cannot unlock it. A supported missing first-use artifact can be runnable
with disclosure; missing runtime or unsupported options cannot.

Actions use their project action-execution router/composition and typed engine
declarations; Copilot uses its router/default model/provider-spend policy;
embeddings use the existing provider/modality catalog, omitting reserved unbuilt
engines. Preserve provider-bound custom embedding IDs with a custom-ID detail
control and existing dimension discovery. A provider placeholder is not runnable.
Missing-key providers remain visible when policy permits setup. Authoritative
empty results stay empty; errors show Retry, never static fallback choices.
Saved unknown IDs remain visible and blocked without silent replacement.

Reads perform no inference, estimates, downloads or runtime startup. Reuse
passive/cached facts except for configured local-LLM endpoint model discovery:
the existing bounded, credential-safe `/api/tags` then `/v1/models` probe is
permitted on catalog load and explicit Recheck, never hover. Its existing owner
supplies the endpoint-qualified model roster and readiness; no new discovery
cache or probe framework is needed. No paid-provider or gateway probe is triggered
by catalog loading. Descriptions stay with the backend model catalog,
prices with pricing, download bytes with the artifact manifest, target features
with execution definitions. Omit unverified speed, quality, size or language data.
When a complete action draft exists, the controller may debounce the existing
`/actions/v1/estimate` for an inspected candidate without committing it. Otherwise
show supported unit pricing or no estimate. No browser pricing formula, new batch
estimator or per-choice LLM call; retain existing consent/billed-price controls.

### Setup authority

Inject one immutable request/project capability value through a `create_app`
callback: `may_author_actions`, `may_run_actions`, `configure_workspace_credentials`,
`configure_project_credentials`, `configure_organization_credentials`,
`manage_model_downloads`, `configure_models_gateway`. The facade receives
booleans, not roles or access services. Missing hosted/Team injection fails closed;
Solo follows the setup routes actually registered. Mutation routes remain the
authority and independently enforce permissions.

Team derives author/run from editor access, project credential changes from
**project owner**, organization changes from its owner predicate, and pulls from
org owner plus enabled pull routes. Cloud injects its existing project access and
organization key-manager predicates (including admin); no ingress/middleware
rewrite. Model pulls/gateway edits remain unavailable where routes do not exist.
Project and organization authority are independent; never infer them in the UI.

Setup union: `api_key`, `models_gateway`, `artifact_download`, `engine_setup`,
`first_use_download`, `instructions`. Mutation descriptors include actual scope
and `can_mutate`; downloads separately carry `can_start`, matching operation and
`blocked_by_operation`. Queue capacity is not authority and a busy queue cannot
block an already-ready engine. Environment-owned configuration is read-only.

## Setup operations and progress

### Provider keys

Reuse existing workspace/project/organization validation and save routes. Offer
only permitted scopes; keep keys out of selector URLs, local storage, logs and
action payloads. Validate the candidate then save with its receipt; failure
preserves the working credential. Make organization saves require/verify receipts
as workspace/project saves already do. Reuse each edition's actual authorization.
Refresh facts on success without selecting/closing. Clear inputs on scope change.

### Explicit downloads

Reuse durable `model_pulls` and workspace activity. Extend its wire projection to
v4 with `operation_kind: artifact|local_model|engine_setup`, `display_name` and
status-aware `capabilities {cancel,retry,remove}`. Update both local and Team wire
mirrors and the existing progress UI together. No remove for HF snapshots,
Ollama or Parakeet where no owned uninstall exists. Unknown byte totals remain
indeterminate; cancellation becomes terminal only after acknowledgement.

Add `POST /api/providers/models/setup` and Team owner-scoped
`POST /api/org/models/setup`, body `{setup_ref}`. Initial allowlisted ref
`engine-setup:parakeet-tdt.local-onnx@1` installs pinned Parakeet plus the VAD needed
by the existing target. One existing pull row/job stores it in `model_ref`; no
child jobs, generic dependency graph or migration. Use the existing supervised
artifact provisioner; recheck cache and skip completed components on retry.
Same-ref requests deduplicate; another active operation returns existing busy
behavior plus that operation. Cache/runtime facts decide readiness, not historic
job completion.

### Models gateway attachment

Use a distinct config resource, not an Ollama endpoint masquerading as a gateway.
Workspace `/api/models-gateway` and Team `/api/org/models-gateway` expose GET
status, POST `/validate`, PUT save. Team GET is member-readable, mutations owner
only. No disconnect control required. Status includes configured/source/origin,
token-configured/masked hint, authority, environment names and optional probe.

Validation takes both candidate `{origin,token}` or neither for explicit Recheck.
Candidate success issues an expiring receipt bound to scope, normalized origin,
exact token and protocol. PUT requires those values and receipt. Validate HTTPS
(or loopback HTTP) using existing origin rules and a bounded authenticated GET
`/capabilities`, redirects disabled, service `frisket-models`, required typed engine
fields. No inference or secret-bearing errors. Failure preserves old config.
Environment URL/token wins read-only; a partially configured nonblank pair errors
instead of silently falling through to stored configuration.

Store workspace pair atomically in the existing private provider-key map; Team
uses its encrypted org environment store, reserving the two names against generic
writes. One connection resolver feeds actual execution and capability probing.
Cached probes key on origin/token fingerprint; Recheck bypasses cache. Queued
workers resolve through trusted org context through a runtime connection port
at the existing credential/composition boundary, never job payloads. Prove saved
configuration reaches queued execution with gateway environment variables unset.
No second secret store or migration. Owned Start/Stop and autostart remain #46.

### Opus automatic preparation

Existing worker-side provisioning owns transfers and retries. Eligibility uses
supported pairs/runtime; web-host cache presence cannot prove worker readiness.
Add an indeterminate first-use hint derived from run params only while status is
`running` and completed=0, and in existing running preview-job progress. Honest copy: “Preparing the
language model if this worker needs it, then translating…” Normal row progress
replaces it when progress starts. No percentage, new run state/column, duplicate
pull job or generic progress-event subsystem. Reuse cancellation/pair errors.
Protect local recheck/provision with a per-artifact filesystem lock to close the
real multi-process promotion race; no distributed-lock abstraction.

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

## Implementation and proof gates

Freeze this spec in Git; independent subagent and Claude exact-ref review precede
implementation. Each writer gets an isolated exact-base worktree and explicit
path roster. Root integrates overlapping app/route registration and generation.
No worker pushes or lands independently.

Lanes: choice DTO/projection/authority; setup/gateway/operation integration; shared
visual component; then caller migration/controller and edition composition.
Split only where paths and dependencies permit. New public behavior begins with
a focused failing test, then implementation and bounded validation.

Required proof:

- Choice route: incomplete draft, active-target options, unknown saved choice,
  authoritative empty list, usage router, no inference/download/startup effects,
  bounded configured-endpoint discovery only, first-use eligibility.
- Authority: viewer read without mutation; project-owner/org-member and
  project-viewer/org-owner cases; real mutation enforcement. Bounded Cloud
  companion injects its own predicates and inherits the public route.
- Setup: receipt binding, failed-save preservation, environment precedence,
  gateway redirect/service/auth failures, saved connection observed by actual
  target and queued runtime without secrets in payloads.
- Downloads: one Parakeet operation, partial retry, dedupe/busy/cancel, truthful
  remove; Opus preparation only on execution, honest progress, same-cache race.
- Shared UI: selection/inspection, scoped async guards, search/recents, save and
  completion, keyboard/focus, independent scrolling and small-screen detail path.
  One offline composed browser journey at desktop/mobile widths.
- Migration: generated, mixed, compare, Copilot and embedding callers including
  run/preview gating and custom IDs. Inspect and delete all replaced pickers.

Use focused suites while iterating, then one composed backend/frontend/build and
relevant integration pass. Sol and Claude review exact code; inspect descendants
proportionally. PR(s), required CI, merge, verify remote ancestry. Completion
requires every listed surface and setup path integrated and merged, not a new
picker beside the old ones.
