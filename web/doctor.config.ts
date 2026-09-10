export default {
  // pdfjs-dist supply-chain exception: Socket's 31/100 traces to the package
  // SHAPE — bundled WASM
  // codecs (openjpeg/jbig2/qcms) + an embedded QuickJS-in-WASM engine for
  // PDF AcroForm JavaScript — not to risk signals (vulnerability 100/100,
  // maintenance 96/100, zero install scripts, empty scripts object). This is
  // Mozilla pdf.js, the same code shipping inside Firefox; replacing it is a
  // trust DOWNGRADE. Accepted with this written justification; revisit if
  // Socket's risk axes (not shape axes) ever degrade.
  // Mechanism: fully ignored. Measured on the score-
  // ceiling spike: as a warning this one finding compressed the score's
  // dynamic range by ~26-32 points, masking real regressions — a score that
  // cannot move is useless as a smell. The acceptance itself is unchanged
  // and lives in this comment; the watch condition remains: re-run a Socket
  // review on any pdfjs-dist major bump, and revisit if Socket's RISK axes
  // (vulnerability/maintenance — not shape axes) ever degrade.
  supplyChain: {
    severity: 'warning',
  },
  ignore: {
    overrides: [
      {
        // The pdfjs-dist accept above, applied: the finding is excluded from
        // scoring entirely so the score keeps its dynamic range as a
        // regression smell. The justification and watch condition live in
        // the header comment; this override is its mechanism.
        files: ['package.json'],
        rules: ['socket/low-supply-chain-score'],
      },
      {
        // actionpanel-decomposition-v1: these dispositions were written for
        // the pre-split ActionPanel.tsx; the ActionForm orchestrator that
        // carries the covered code lives in action-panel/ActionForm.tsx now.
        files: ['src/components/action-panel/ActionForm.tsx'],
        rules: [
          // Temporary noise control for the known ActionForm cleanup. The
          // decomposition deliberately KEEPS ActionForm large: Research/joins/run
          // footer stay inside the orchestrator until they have truthful
          // seams, so no-giant-component still fires here by design.
          'react-doctor/no-giant-component',
          'react-doctor/no-pass-live-state-to-parent',
          // prefer-tag-over-role findings, all
          // KEEP (linter's suggested tag is wrong for every one — see the
          // shared 'address'/'datalist' writeups on the sibling overrides
          // below):
          // - ActionForm.tsx role="group" (engine-tier chips; the source-mode
          //   segmented toggles render the same shape through PanelPrimitives.tsx's
          //   SegmentedToggle) — all button-toolbar groups. <address> (the
          //   linter's suggestion) is for contact info — a clear misfire.
          //   role="group" + aria-label is the correct, minimal ARIA for a
          //   labeled button toolbar; there is no HTML5 tag that better
          //   expresses "toolbar of toggle buttons" (a <fieldset> would need
          //   a <legend> that changes the visual chrome for a non-form
          //   control).
          'react-doctor/prefer-tag-over-role',
        ],
      },
      {
        // The save-to-column picker (OutputNameCombobox) moved here from
        // ActionPanel.tsx in the actionpanel-decomposition-v1 leaf cut; the
        // disposition is unchanged from its pre-split writeup:
        // - TargetSaveToControl.tsx:130 role="listbox" — paired with a real
        //   <datalist> already in the same control (typeahead) PLUS this
        //   rich role="option" button list (name + type + AI badge) for
        //   pointer browsing. <datalist> cannot render that; a <select>
        //   would drop the dual affordance. Same shape as PanelSelect.tsx's
        //   documented dual-select pattern. KEEP.
        //
        // actionform-accessible-name-combobox-correction-v1: Doctor does not
        // model the implicit combobox role that HTML-AAM gives input[list],
        // so it incorrectly reports aria-expanded/aria-controls as unsupported.
        // Keep the native datalist and implicit role; an explicit identical role
        // would instead be redundant. The custom rich listbox is keyboard-real.
        // Its pointer-hover handler intentionally synchronizes the same active
        // option used by aria-activedescendant; CSS hover cannot update that
        // accessibility state, so no-event-handler is a false positive here.
        files: ['src/components/action-panel/TargetSaveToControl.tsx'],
        rules: [
          'react-doctor/prefer-tag-over-role',
          'react-doctor/role-supports-aria-props',
          'react-doctor/no-event-handler',
        ],
      },
      {
        // The sheet tab's nested "sheet info"
        // span (App.tsx:1990, role="button") — already investigated and left
        // alone by a prior lane (commit f4802d56b, "doctor: fix App.tsx
        // mechanicals ... document 4 verified-unsafe findings left alone"):
        // a real <button> can't nest inside its parent tab's own <button>
        // (the naive fix traded prefer-tag-over-role for doctor's own
        // html-no-nested-interactive).
        //
        // The work-split resize seam this entry ALSO used to cover
        // (App.tsx:3057, role="separator") moved out of this file
        // (role-primitives-consolidation-v1): App.tsx now renders
        // <ResizeSeam>, so its separator finding — and this entry's old
        // reasoning for it — lives at the ResizeSeam.tsx entry below.
        files: ['src/App.tsx'],
        rules: ['react-doctor/prefer-tag-over-role'],
      },
      {
        // The four resize seams that used to
        // duplicate this same role="separator" shell (App.tsx:3057,
        // DiscoverPanel.tsx:317, MediaCompareShell.tsx:308,
        // InspectDetailColumn.tsx:105 — each previously its own allowlist
        // entry) now render through one <ResizeSeam> component
        // (src/components/ResizeSeam.tsx). The reasoning is unchanged: the
        // seam is keyboard-operable (tabIndex + onKeyDown + onPointerDown);
        // converting to <hr> trades prefer-tag-over-role for
        // no-noninteractive-element-interactions +
        // no-noninteractive-tabindex (<hr> is non-interactive semantics,
        // incompatible with a focusable/keyboard resize control) — verified
        // via a doctor re-run. One allowlist entry now covers all four call
        // sites' shared markup instead of four near-identical ones.
        files: ['src/components/ResizeSeam.tsx'],
        rules: ['react-doctor/prefer-tag-over-role'],
      },
      {
        // doctor-semantic-tags-v1: the bottom-dock run-progress bar
        // (WorkbenchBottomDock.tsx:420, role="progressbar" +
        // aria-valuemin/max/now) is already COMPLETE, correct ARIA —
        // identical to what a native <progress> would expose. The blocker
        // is visual, not accessibility: the element renders BOTH a
        // determinate fill (width: N%, .workbench-bottom-progress-fill) AND
        // an indeterminate state with a custom sliding-bar keyframe
        // animation (styles.css:990-997) sized to the app's 5px/96px amber
        // (--monitor) design. A native <progress> cannot host that fill
        // markup as a child (browsers that support <progress> ignore its
        // content, which is fallback-only) — reproducing the same pixel-
        // precise fill + custom slide animation would require vendor-
        // prefixed pseudo-element CSS (::-webkit-progress-value,
        // ::-moz-progress-bar) with well-known cross-browser inconsistency,
        // for zero ARIA gain. KEEP.
        //
        // derived-state-deps-v1: no-derived-useState at :484
        // (useState(activeTabPlacementId)) is a false positive. That state is
        // react.dev's "storing previous props" change-DETECTOR — it is never
        // rendered (the shown value, activeContribution, is a fresh useMemo over
        // activeTabPlacementId) and is updated during render whenever the prop
        // moves. Nothing goes stale; the site's comment carries the full
        // reasoning. KEEP.
        files: ['src/workbench/WorkbenchBottomDock.tsx'],
        rules: [
          'react-doctor/prefer-tag-over-role',
          'react-doctor/no-derived-useState',
        ],
      },
      {
        // doctor-semantic-tags-v1: ModelPicker's two role="listbox" panes
        // (:328 search results, :362 per-provider model list) render rich
        // rows (avatar, name, reachability chip) inside a positioned popover
        // menu — the same "search input + rich listbox results" combobox
        // pattern PanelSelect.tsx documents. <datalist> (the linter's
        // suggestion) is a plain-text autocomplete affordance; it cannot
        // render rich rows. KEEP the listbox role — correct ARIA for a
        // widget <datalist> structurally cannot express.
        //
        // CORRECTED: this KEEP
        // originally claimed the ARIA was fully correct, but renderOption's
        // <button> children (ModelPicker.tsx:253) carried neither
        // role="option" nor aria-selected — both listboxes owned
        // non-conforming option children, a real (minor) defect the
        // per-site review missed. Fixed by adding role="option" +
        // aria-selected={model.id === value} to renderOption; the listbox
        // container role itself was never in question.
        files: ['src/components/ModelPicker.tsx'],
        rules: ['react-doctor/prefer-tag-over-role'],
      },
      {
        // doctor-semantic-tags-v1: PanelSelect.tsx:129's role="listbox"
        // popover is the file's OWN documented design (see the file's
        // top-of-file comment): a real <select> stays in the DOM as the
        // keyboard/test/value-semantics trigger, and this listbox is a
        // pointer-only custom popover with rich option rows (descriptions,
        // AI ⚡ markers, a selected-check) that <datalist> cannot render.
        // KEEP.
        files: ['src/components/PanelSelect.tsx'],
        rules: ['react-doctor/prefer-tag-over-role'],
      },
      {
        // doctor-semantic-tags-v1: MultiColumnPicker.tsx has two
        // prefer-tag-over-role findings, both KEEP:
        // - :92 role="group" (the chip-box combobox trigger: selected-column
        //   chips + filter input) — <address> is a contact-info tag, a clear
        //   misfire; role="group" + aria-label is the correct, minimal ARIA
        //   for grouping the chips+input as one labeled field.
        // - :154 role="listbox" (aria-multiselectable, the portal dropdown)
        //   — rich checkbox-like multi-select rows with AI ⚡ markers;
        //   <datalist> cannot render rich rows or support multi-select.
        files: ['src/components/MultiColumnPicker.tsx'],
        rules: ['react-doctor/prefer-tag-over-role'],
      },
      {
        // doctor-semantic-tags-v1: PanelPrimitives.tsx:189's role="group"
        // is SegmentedToggle's outer wrapper (a labeled toolbar of toggle
        // buttons, aria-pressed). <address> (the linter's suggestion) is for
        // contact info — a clear misfire. role="group" + aria-label is the
        // correct, minimal ARIA for a labeled button toolbar. KEEP.
        files: ['src/components/PanelPrimitives.tsx'],
        rules: ['react-doctor/prefer-tag-over-role'],
      },
      {
        // doctor-semantic-tags-v1: AnswersView.tsx:335's role="listbox" (the
        // row list, tabIndex + onScroll + onKeyDown) is a virtualized/
        // windowed row scroller (a direct port of DocumentView.tsx's list —
        // see AnswersView.tsx:40) — <datalist> cannot virtualize or render
        // rich rows. Originally dispositioned under F4
        // (doctor-answersview-race-fix-v1, AnswersView.tsx:317 pre-refactor)
        // and re-homed here per F3's site list once F4 shipped. KEEP.
        //
        // derived-state-deps-v1: no-prop-callback-in-effect at :131 is a false
        // positive. The `queryRows` prop is a SERVER data-fetcher (App.tsx wires
        // it to api.getSheetData) that returns a Promise — not a parent state
        // setter. The effect loads the first page on listKey/queryRows change and
        // forces no ancestor re-render, so this is the ordinary "fetch data in an
        // effect" pattern, not the shared-state-sync anti-pattern the rule
        // targets. KEEP.
        files: ['src/workbench/AnswersView.tsx'],
        rules: [
          'react-doctor/prefer-tag-over-role',
          'react-doctor/no-prop-callback-in-effect',
        ],
      },
      {
        // derived-state-deps-v1: DiscoverTabStrip's visibleCount
        // (DiscoverPanel.tsx:70, useState(tabs.length)) is DOM-measurement
        // state, not a copy of tabs.length. The overflow split is computed from
        // live offsetWidth/clientWidth in a useLayoutEffect (recompute), so it is
        // not render-derivable; tabs.length is only the first-paint seed and
        // recompute() re-runs on every tabs change (that effect's dep is [tabs])
        // synchronously before paint, so the seed never shows stale. The site
        // comment carries the full reasoning. no-derived-useState is a false
        // positive here. KEEP.
        files: ['src/workbench/DiscoverPanel.tsx'],
        rules: ['react-doctor/no-derived-useState'],
      },
      {
        // route.project(next) runs inside the URL→routeStore projection effect.
        // no-pass-data-to-parent is
        // a false positive: route.project writes an EXTERNAL store handle, not a
        // parent React setState — it forces no ancestor re-render and applies its
        // own structural-equality bail. The whole effect is a projection
        // with side effects (window.location staleness read, openSplit hydrate,
        // history normalize) that cannot be render-derived. KEEP.
        files: ['src/bind/useRouteProjection.ts'],
        rules: ['react-doctor/no-pass-data-to-parent'],
      },
      {
        // derived-state-deps-v1: mediaCompareSession.ts:424 — no-derived-state
        // on the auto-run effect's runProgress. False positive: runProgress is an
        // async-completion accumulator (`total` captured at run launch, `done`
        // incremented in each runColumnForDoc's finally), not a value derivable
        // during render from current docs state. See the runProgress declaration
        // and auto-run effect comments. KEEP.
        //
        // Scope note: only no-derived-state is allowlisted. The
        // no-pass-live-state-to-parent / no-pass-data-to-parent findings in this
        // file (the :684 onSessionChange effect, plus the :424 misfire) are left
        // red DELIBERATELY, per the onSessionChange writeup at
        // mediaCompareSession.ts (doctor-compare-render-hotpath-v1): a
        // cross-boundary render-time rewrite trips React's "cannot update a
        // component while rendering a different component" (a regression, not a
        // stale→fresh fix), and a genuine future fix — lifting `docs` state into
        // the ancestor — remains possible, so the finding is kept visible.
        files: ['src/workbench/mediaCompareSession.ts'],
        rules: ['react-doctor/no-derived-state'],
      },
      {
        // doctor-semantic-tags-v1: DocumentView.tsx:226's role="listbox"
        // (the document list, tabIndex + onScroll + onKeyDown) is the
        // virtualized/windowed row scroller AnswersView.tsx's own listbox
        // was ported from (AnswersView.tsx:40, "direct port of
        // DocumentView.tsx's list"). Same KEEP rationale: <datalist> cannot
        // virtualize.
        files: ['src/workbench/DocumentView.tsx'],
        rules: ['react-doctor/prefer-tag-over-role'],
      },
      {
        // doctor-semantic-tags-v1: DocumentReader.tsx:223's role="group"
        // (aria-label="Page navigation") wraps a MIXED toolbar — page
        // prev/next + page indicator AND zoom out/in — not pure navigation,
        // so a <nav> landmark (the more specific real tag that would fit
        // the page-turn buttons alone) would misrepresent the zoom controls
        // as navigation. <address> (the linter's suggestion) is a clear
        // misfire either way. role="group" + aria-label is the correct,
        // minimal ARIA for this mixed-purpose toolbar. KEEP.
        files: ['src/workbench/DocumentReader.tsx'],
        rules: ['react-doctor/prefer-tag-over-role'],
      },
      {
        // sign-in-request-form-split-v1: SignIn keeps six independently
        // evolving lifecycles (email, busy/sent/error, sender metadata, and
        // the invited-arrival URL marker). They do not form one transition
        // state machine, so useReducer would obscure rather than clarify the
        // request-form behavior.
        files: ['src/components/SignIn.tsx'],
        rules: ['react-doctor/prefer-useReducer'],
      },
      {
        files: ['playwright/e2e-duration-server.mjs'],
        rules: [
          // The wrapper is an entry point spawned by playwright.local-stack.ts
          // rather than imported from a JavaScript entry point. Its readiness
          // loop is intentionally sequential bounded polling of one URL.
          'deslop/unused-file',
          'react-doctor/async-await-in-loop',
        ],
      },
      {
        // playwright.team-stack.ts is
        // imported (by playwright.team.config.ts, exactly like
        // playwright.local-stack.ts is imported by the default
        // playwright.config.ts) — but that importer is reached only via an
        // explicit `--config=playwright/playwright.team.config.ts` CLI flag,
        // not Playwright's auto-discovered default config name, so deslop's
        // reachability scan never walks into it.
        files: ['playwright/playwright.team-stack.ts'],
        rules: ['deslop/unused-file'],
      },
      {
        // Every file
        // under first-party/ default-exports exactly one CommandDescriptor,
        // consumed ONLY via import.meta.glob('./first-party/*.command.ts',
        // { eager: true }) in registry.ts:93 (workspace-substrate-8-glob-
        // registration-v1) — deslop's static analysis cannot see a
        // glob-only importer, so every `export default` here reads as
        // unused. THE GUARD, not a blanket trust call: this is not "glob
        // dirs are exempt" — core/commands/reachability.test.ts's F7
        // discriminator proves every command this glob discovers is
        // ACTUALLY reachable (palette exposure | Act-menu switch map |
        // hostContext.navigation | an explicit justified allowlist) before
        // this override was written. It already caught and removed one
        // real orphan (`workView.set` — glob-registered, union-typed, but
        // never wired to any dispatch() call site; see types.ts's
        // WorkspaceCommand doc-comment) instead
        // of hiding it here. If a future command file fails the
        // discriminator, that test goes red — fix the command, not this
        // override.
        files: ['src/core/commands/first-party/*.command.ts'],
        rules: ['deslop/unused-export'],
      },
      {
        // Same shape, the work-view sibling glob (core/selectors/views/
        // registry.ts's import.meta.glob('./*.view.ts', { eager: true })).
        // Unlike the command family, every view's `export default` is
        // provably live without a per-entry reachability check: registry.ts
        // folds ALL of WORK_VIEW_DESCRIPTORS into WORK_VIEW_KINDS/
        // WORK_VIEW_TITLES via blanket Object.keys/Object.values (not
        // selective per-kind indexing), both consumed by real production
        // code (core/selectors/workView.ts:91, useWorkspaceModel.tsx) — so
        // an orphaned view.ts file structurally cannot exist the way an
        // unwired command could; there is no selective branch for a view to
        // fall through.
        files: ['src/core/selectors/views/*.view.ts'],
        rules: ['deslop/unused-export'],
      },
      {
        // resolveAnchoredPosition is the hook module's exported pure geometry
        // primitive even though useAnchoredPosition is its only in-tree caller.
        files: ['src/hooks/useAnchoredPosition.ts'],
        rules: ['deslop/unused-export'],
      },
      {
        // editions-posture-contract-v1: OPEN_EDITION_POSTURES is a test-pinned
        // base vocabulary used by edition contract checks. Downstream edition
        // ids are opaque descriptors and intentionally do not live here.
        files: ['src/editions/posture.ts'],
        rules: ['deslop/unused-export'],
      },
      {
        // MediaCompareShell.tsx's
        // renderRemoteGateBody/renderOptionFields/renderSourcePeek are the
        // compare shell's render-prop contract — the two-tab (OCR/transcribe)
        // parameterization DESIGN, not a bug. KEPT as render props rather
        // than converted to slots/children (that conversion is a low-payoff
        // Maintainability-only churn across 3 consumers + specs, deferred per
        // the doc's F6 verdict). `no-render-in-render` pattern-matches any
        // `renderFoo()` call written directly in JSX regardless of which
        // component's body it sits in, so it still flags these three call
        // sites even after the real fix:
        //   - :231 (renderSourcePeek, formerly ~293/295) — the ONE genuinely
        //     hot site (MediaCompareBody re-renders on every vote/nav-token
        //     change). PROVEN fixed by extracting SourcePeekPane + memo with a
        //     comparator over doc-identity/nav/width (never votes/runs): a
        //     render-count probe measured 20 extra source-peek invocations
        //     across 5 vote clicks before the fix, 0 after. The remaining
        //     `renderSourcePeek(activeDoc, nav)` call is INSIDE that memoized
        //     component — the call the linter flags is exactly the one that
        //     no longer re-executes on vote churn.
        //   - :131 (renderRemoteGateBody), :148 (renderOptionFields) — inside
        //     ConfigureVariantPopover, which only re-renders while that one
        //     popover is open (no vote-state prop reaches it at all) — no
        //     hot-path case to fix here the way there was for the source peek.
        files: ['src/workbench/MediaCompareShell.tsx'],
        rules: ['react-doctor/no-render-in-render'],
      },
      {
        // AnnotatedTextReader's scroll-to-occurrence effect. no-event-handler
        // is a false positive here — the
        // event it looks like it is mirroring happens in a DIFFERENT component,
        // about a DIFFERENT document.
        //
        // Clicking an occurrence in the mention-detail panel can name a mark in
        // a document the reader has not fetched yet. The scroll therefore
        // cannot happen in any click handler: the target element does not exist
        // until this component's own fetch settles and the partition renders.
        // The effect's deps are exactly that pair (the active occurrence and
        // the settled fetch), which is the ordinary "synchronize DOM state with
        // freshly-rendered content" case, not event logic displaced into an
        // effect. Moving it into the panel's handler would mean the panel
        // reaching into the reader's DOM and racing its fetch. KEEP.
        files: ['src/workbench/AnnotatedTextReader.tsx'],
        rules: ['react-doctor/no-event-handler'],
      },
      {
        // replay-accept-surface-v1: the pure replay-pending surface logic lives
        // in the fast-refresh-safe plain module src/grid/replayPending.ts (the
        // repo idiom — non-component exports split out of a component .tsx).
        // Product code imports it from there directly. columnAnnotations.tsx
        // re-exports those six functions SOLELY so the admission-frozen surface
        // check (src/state/replayPendingSurface.test.ts) can read the vocabulary
        // through the columnAnnotations idiom it rides; that frozen test's import
        // path cannot change without invalidating recorded semantic-red evidence.
        // The single non-component re-export block is the only finding here.
        files: ['src/grid/columnAnnotations.tsx'],
        rules: ['react-doctor/only-export-components'],
      },
    ],
  },
};
