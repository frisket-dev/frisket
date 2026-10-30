# Author a guide

`guides/recipes/*.json` owns each recipe's stable ID, material, sample revision,
ordered steps, action IDs, parameter values, semantic targets and capture IDs.
The site owns prose in `site/src/_data/guideContent.json`, keyed by recipe, step
and setting IDs. Do not repeat step order or recommended values in site prose.

1. Inspect the current action catalog and actual frontend controls. Use the
   frontend presentation export for visible labels and menu paths. A backend
   action title alone may differ from the button.
2. Choose an existing versioned sample. Check its actual files and provenance.
   Use source-supported expected fields. Companion metadata is not necessarily
   stated in the source document.
3. Add a plan and matching editorial entry. Use `material` to join material hubs
   and `tasks` in editorial metadata to join the task index. Navigation and
   reverse action links are generated from those relationships.
4. Reuse semantic WalkthroughTarget bindings. A setting can omit `value` while
   its runtime choice is unresolved; do not put a pretend selection in a
   screenshot. Output destination settings omit `param` because output names
   are not action parameters.
5. Run the ordinary UI journey against that sample. Capture each named checkpoint
   through the strict helper. It checks control labels and selected values, then
   writes the PNG and measured annotation rectangles together. Do not hand-edit
   image coordinates or reuse a picture from another sample.
6. Review the real result against an independent answer key. Fill engine choices
   and quality examples from recorded evidence. Mark unrun examples as pending;
   an empty result is not proof of quality. `testedIn` lists completed platform
   verification only, so new plans start with an empty list.
7. Promote a complete pinned bundle, build the site and run its docs acceptance
   checks. Inspect the affected pages, deep links and screenshot pins. Publishing
   is a separate coordinator action.

The initial court recipe extracts `case_number`, `filed_at`, `plaintiff` and
`defendant_as_filed`. `sample_docket_csv()` is the independent companion key;
its `court`, `status` and normalized `document_type` fields are deliberately not
extraction targets because the PDFs do not consistently state those values.
The generated Riverton filings are fictional practice data. Council excerpts
are CC0 recordings with their source links in the Council meetings sheet.

Current checkpoint IDs:

- `court-check-text`, `court-extract-settings`, `court-extract-fields`,
  `court-extract-instruction`, `court-sample-run`,
  `court-expected-join`, `court-comparison`, `court-run-settings`, `court-results`
- `council-engine-comparison` (always visible in the recipe),
  `council-transcribe-settings`, `council-transcribe-outputs`,
  `council-timed-transcript`, `council-results`

The court comparison needs a real persisted action with output columns. Select
the three named sample filings, optionally Preview, then Run on selected before
joining Expected. A temporary Preview cannot be selected as the action to check.
Only after comparing this small sample should you run all six filings.

The court comparison joins the imported Expected sheet by original PDF filename
and maps only the four extraction fields. Use exact text matching and the date
rule for `filed_at`. The recordings comparison uses the same first 30 seconds
with two available engines before the full transcription. The recorded court setup uses source `text`, engine `llm` and model
`gemini/gemini-3.5-flash-lite`; its filename join uses target `filename`.
The council comparison uses `faster_whisper` with `base` and
`openrouter/microsoft/mai-transcribe-2`; full-sheet transcription uses the local
`faster_whisper` base model. Preserve actual results during capture.

Plans describe supported steps; they do not execute jobs, authorize payment,
or bypass the app's normal model/download and cost confirmation paths.

PDF import choices use accessible radio targets: `{kind: "radio", group, value}`.
For these controls, capture resolves the radio group/name and value, checks its
associated visible option label and checked state, and measures the same control.
The `None` choice in this recipe does not execute the OCR action.
