export type WalkthroughAdvance = 'click-target' | 'next-button';

export interface WalkthroughRichInstruction {
  readonly lead: string;
  readonly code: string;
  readonly explanation?: string;
  readonly learnMore?: {
    readonly label: string;
    readonly href: string;
  };
}

export type WalkthroughInstruction = string | WalkthroughRichInstruction;

export type WalkthroughTarget =
  | { readonly kind: 'action-tab'; readonly id: string }
  | { readonly kind: 'action'; readonly id: string }
  | { readonly kind: 'command'; readonly id: string }
  | { readonly kind: 'discover-tab'; readonly id: string }
  | { readonly kind: 'sheet'; readonly id: string }
  | { readonly kind: 'grid-column'; readonly id: string }
  | { readonly kind: 'field'; readonly id: string }
  | { readonly kind: 'view'; readonly id: string }
  | { readonly kind: 'element'; readonly id: string }
  | { readonly kind: 'tour'; readonly id: string }
  | { readonly kind: 'local-endpoint'; readonly label: string; readonly part: 'row' | 'guidance' }
  | { readonly kind: 'completed-run'; readonly target: WalkthroughTarget };

export type WalkthroughFillSpec =
  | {
      readonly kind: 'action-form';
      readonly actionKind: string;
      readonly label?: string;
      readonly values: Readonly<Record<string, unknown>>;
      readonly outputNames?: Readonly<Record<string, string>>;
      readonly outputPrefix?: string;
      readonly sheetName?: string;
      readonly rightSheetName?: string;
    }
  | {
      readonly kind: 'project-ask';
      readonly label?: string;
      readonly question: string;
    }
  | {
      readonly kind: 'feed-import';
      readonly label?: string;
      readonly url: string;
      readonly name?: string;
      readonly feedKind?: string;
    };

export function walkthroughFillerKey(spec: WalkthroughFillSpec): string {
  switch (spec.kind) {
    case 'action-form': return `action-form:${spec.actionKind}`;
    case 'project-ask': return 'project-ask';
    case 'feed-import': return 'feed-import';
  }
}

export interface WalkthroughStep {
  readonly id: string;
  readonly title: string;
  readonly instruction: WalkthroughInstruction;
  readonly target: WalkthroughTarget;
  readonly expected?: string;
  readonly advance: WalkthroughAdvance;
  readonly fill?: WalkthroughFillSpec;
}

export interface WalkthroughDefinition {
  readonly id: string;
  readonly title: string;
  readonly description: string;
  readonly requirements?: string;
  /** Omitted means visible in every edition. */
  readonly editions?: readonly string[];
  readonly steps: readonly WalkthroughStep[];
}

export const PROJECT_ASK_WALKTHROUGH: WalkthroughDefinition = {
  id: 'project-ask',
  title: 'Ask across your whole project',
  description: 'Ask one question across Dispatches, Contracts, and Court filings, then jump straight to cited rows.',
  requirements: 'Requires an available model provider.',
  steps: [
    {
      id: 'open-project-ask',
      title: 'Open Project Ask',
      instruction: 'Click Ask in the top bar to open the cross-sheet investigation assistant.',
      target: { kind: 'element', id: 'chrome-ask-toggle' },
      expected: 'The Ask panel opens with your entire project in scope.',
      advance: 'click-target',
    },
    {
      id: 'enter-project-question',
      title: 'Ask across sheets',
      instruction: 'Ask: "Which vendors appear in both emergency contracts and lawsuits, and what are the open questions?" Use Fill for me to populate the composer, or type your own question.',
      target: { kind: 'element', id: 'ask-composer-input' },
      fill: {
        kind: 'project-ask',
        question: 'Which vendors appear in both emergency contracts and lawsuits, and what are the open questions?',
      },
      expected: 'The question is ready in the composer; nothing has been sent yet.',
      advance: 'next-button',
    },
    {
      id: 'send-project-question',
      title: 'Send your question',
      instruction: 'Confirm your model, then click Send to search Dispatches, Contracts, and Court filings together.',
      target: { kind: 'element', id: 'ask-send-button' },
      expected: 'Project Ask searches across sheets and streams a cited answer.',
      advance: 'click-target',
    },
    {
      id: 'inspect-project-citations',
      title: 'Follow cross-sheet citations',
      instruction: 'Read the answer and click any inline citation badge to jump directly to the supporting row in Dispatches, Contracts, or Court filings.',
      target: { kind: 'element', id: 'ask-dock' },
      expected: 'Every claim ties back to a real row you can inspect and verify.',
      advance: 'next-button',
    },
  ],
};

export const LAWSUIT_DOCUMENT_WALKTHROUGH: WalkthroughDefinition = {
  id: 'lawsuit-documents',
  title: 'Read, OCR & verify lawsuit PDFs',
  description: 'Inspect born-digital and scanned filings, run OCR for clickable PDF highlights, extract cited facts, review them, and link an answer key.',
  requirements: 'Requires an available OCR engine and model provider.',
  steps: [
    {
      id: 'open-filings',
      title: 'Open Court filings',
      instruction: 'Click Court filings. Four of these six pleadings are born-digital PDFs and two are tilted flatbed scans with no embedded text layer.',
      target: { kind: 'sheet', id: 'Court filings' },
      advance: 'click-target',
    },
    {
      id: 'open-document-view',
      title: 'Switch to Document view',
      instruction: 'Click Document to read the original court pleadings side by side with the filing list.',
      target: { kind: 'view', id: 'document' },
      expected: 'The selected pleading opens with its full court caption and clerk stamp.',
      advance: 'click-target',
    },
    {
      id: 'inspect-pdf',
      title: 'Compare born-digital and scanned filings',
      instruction: 'Check the pleading paper, boxed caption, and FILED stamp. Switch to the Water Alliance petition or Patel complaint to see a scanned image-only PDF with no selectable text.',
      target: { kind: 'element', id: 'document-pdf' },
      expected: 'You can see why text-only conversion alone cannot read scanned pages.',
      advance: 'next-button',
    },
    {
      id: 'open-media-tab',
      title: 'Open Media (To Markdown vs. OCR)',
      instruction: 'Click Media. Frisket offers both To Markdown (fast, lossless conversion for born-digital DOCX, HTML, and searchable PDFs) and OCR (reads pixels on scanned PDFs AND records word bounding boxes for clickable PDF highlights).',
      target: { kind: 'action-tab', id: 'media' },
      advance: 'click-target',
    },
    {
      id: 'open-ocr',
      title: 'Choose OCR',
      instruction: 'Click OCR so all six filings—including the two scanned image PDFs—gain searchable text and clickable highlight boxes.',
      target: { kind: 'action', id: 'media.ocr' },
      advance: 'click-target',
    },
    {
      id: 'configure-ocr',
      title: 'Configure the OCR source and output',
      instruction: 'Use media as the source column and name the text output filing_text (or click Fill for me).',
      target: { kind: 'field', id: 'field-source' },
      fill: {
        kind: 'action-form',
        actionKind: 'media.ocr',
        values: { source: 'media' },
        outputPrefix: 'filing_text',
      },
      advance: 'next-button',
    },
    {
      id: 'choose-ocr-engine',
      title: 'Confirm an OCR engine',
      instruction: 'Confirm an available OCR engine (such as local RapidOCR) in the engine picker.',
      target: { kind: 'tour', id: 'action-engine-selector' },
      advance: 'next-button',
    },
    {
      id: 'open-grid-before-ocr',
      title: 'Return to Grid before running',
      instruction: 'Click Grid so you can see filing_text and its word-box layer populate beside each PDF.',
      target: { kind: 'view', id: 'grid' },
      advance: 'click-target',
    },
    {
      id: 'run-ocr',
      title: 'Run OCR on all six filings',
      instruction: 'Review your settings, then click Run to OCR every PDF.',
      target: { kind: 'field', id: 'generated-action-run' },
      expected: 'filing_text and its word-coordinate boxes populate for both digital and scanned PDFs.',
      advance: 'click-target',
    },
    {
      id: 'review-ocr-text',
      title: 'Inspect the OCR text',
      instruction: 'Scan filing_text in Grid. Every filing—including the two scanned image PDFs—now has clean, searchable text ready for extraction.',
      target: { kind: 'completed-run', target: { kind: 'element', id: 'grid' } },
      advance: 'next-button',
    },
    {
      id: 'open-analyze-extract',
      title: 'Open Analyze',
      instruction: 'Click Analyze to extract structured case facts with clickable citations back to the PDFs.',
      target: { kind: 'action-tab', id: 'analyze' },
      advance: 'click-target',
    },
    {
      id: 'open-structured-extract',
      title: 'Choose Extract fields',
      instruction: 'Click Extract fields to create plaintiff, defendant, and requested_relief columns in one pass.',
      target: { kind: 'action', id: 'map.extract' },
      advance: 'click-target',
    },
    {
      id: 'configure-filing-extract',
      title: 'Define cited extraction fields',
      instruction: 'Read from filing_text, define plaintiff, defendant, and requested_relief, and keep Cite sources on. Click Fill for me to populate all fields cleanly.',
      target: { kind: 'field', id: 'output-fields-label' },
      fill: {
        kind: 'action-form',
        actionKind: 'map.extract',
        values: {
          source: ['filing_text'],
          fields: [
            { name: 'plaintiff', type: 'text', description: 'Plaintiff or petitioner name exactly as filed' },
            { name: 'defendant', type: 'text', description: 'Defendant or respondent name exactly as filed' },
            { name: 'requested_relief', type: 'text', description: 'One-sentence summary of the remedy requested from the court' },
          ],
          instruction: 'Extract the plaintiff and defendant exactly as filed and summarize the requested relief in one sentence. Leave a field blank rather than guessing.',
          grounding: { enabled: true, citation_required: false },
        },
      },
      advance: 'next-button',
    },
    {
      id: 'choose-filing-model',
      title: 'Choose a model',
      instruction: 'Choose an available model for structured extraction.',
      target: { kind: 'tour', id: 'action-engine-selector' },
      advance: 'next-button',
    },
    {
      id: 'run-filing-extraction',
      title: 'Extract the filing facts',
      instruction: 'Review the filled fields and citations toggle, then click Run.',
      target: { kind: 'field', id: 'generated-action-run' },
      expected: 'plaintiff, defendant, and requested_relief populate with clickable PDF citations.',
      advance: 'click-target',
    },
    {
      id: 'open-review-queue',
      title: 'Open the Review queue',
      instruction: 'Click Review in the status bar to verify extracted facts alongside highlighted PDF passages.',
      target: { kind: 'completed-run', target: { kind: 'element', id: 'review-queue-button' } },
      expected: 'The full-screen Review modal opens with the source PDF beside each generated field.',
      advance: 'click-target',
    },
    {
      id: 'verify-in-review',
      title: 'Verify and approve a row',
      instruction: 'Click a field to highlight its cited passage directly on the PDF, then accept or edit a row. Close Review before the final accuracy step.',
      target: { kind: 'element', id: 'review-output-panel' },
      expected: 'Human verification stays attached to the generated column.',
      advance: 'next-button',
    },
    {
      id: 'open-sheets-expected',
      title: 'Check accuracy with an answer-key sheet',
      instruction: 'Click Sheets. When you have ground-truth data in a separate sheet—like Court docket—you can score extractions directly against it (unlike Judge results, which grades against rubric guidelines).',
      target: { kind: 'action-tab', id: 'tables' },
      advance: 'click-target',
    },
    {
      id: 'open-join-expected',
      title: 'Choose Join expected answers',
      instruction: 'Click Join expected answers to link Court docket as your ground-truth answer key.',
      target: { kind: 'command', id: 'join-expected' },
      advance: 'click-target',
    },
    {
      id: 'link-docket-answers',
      title: 'Link Court docket as expected answers',
      instruction: 'Select Court docket, match pdf_filename to filename, map plaintiff and defendant_as_filed, and click Join to inspect exact accuracy in Compare.',
      target: { kind: 'element', id: 'join-expected-dialog' },
      expected: 'Your extracted facts are now linked to ground-truth docket entries for side-by-side accuracy checks.',
      advance: 'next-button',
    },
  ],
};

export const CIVIC_AI_TRIAGE_WALKTHROUGH: WalkthroughDefinition = {
  id: 'civic-ai-triage',
  title: 'Triage stories, Views & Judge',
  description: 'Classify stories, save a filtered View, run Ask only on that View, and grade outputs against editorial guidelines with Judge.',
  requirements: 'Requires an available model provider.',
  steps: [
    {
      id: 'open-dispatches',
      title: 'Open Dispatches',
      instruction: 'Click Dispatches. These 24 civic stories span contracts, courts, housing, transit, and public records.',
      target: { kind: 'sheet', id: 'Dispatches' },
      advance: 'click-target',
    },
    {
      id: 'open-ai-grid',
      title: 'Start in Grid',
      instruction: 'Click Grid so generated columns remain visible as they populate.',
      target: { kind: 'view', id: 'grid' },
      advance: 'click-target',
    },
    {
      id: 'open-analyze-classify',
      title: 'Open Analyze',
      instruction: 'Click Analyze to classify every dispatch into one consistent topic.',
      target: { kind: 'action-tab', id: 'analyze' },
      advance: 'click-target',
    },
    {
      id: 'open-classify',
      title: 'Choose Classify',
      instruction: 'Click Classify to assign one civic beat label per story.',
      target: { kind: 'action', id: 'map.classify' },
      advance: 'click-target',
    },
    {
      id: 'configure-classify',
      title: 'Set source and civic categories',
      instruction: {
        lead: 'Classify story into',
        code: 'contracts, courts, housing, health, transit, education, environment, public safety, other',
        explanation: 'Click Fill for me to populate the source column, labels, and topic column name.',
      },
      target: { kind: 'field', id: 'classify-labels' },
      fill: {
        kind: 'action-form',
        actionKind: 'map.classify',
        values: {
          source: ['story'],
          fields: [
            {
              name: 'topic',
              type: 'category',
              labels: ['contracts', 'courts', 'housing', 'health', 'transit', 'education', 'environment', 'public safety', 'other'],
              description: '',
            },
          ],
        },
      },
      advance: 'next-button',
    },
    {
      id: 'run-classify',
      title: 'Categorize the stories',
      instruction: 'Confirm the engine or model, then click Run.',
      target: { kind: 'field', id: 'generated-action-run' },
      expected: 'Each story receives a comparable topic label.',
      advance: 'click-target',
    },
    {
      id: 'open-filter-sidebar',
      title: 'Filter to one beat',
      instruction: 'Click Filter to narrow the sheet before running deeper questions.',
      target: { kind: 'completed-run', target: { kind: 'element', id: 'filter-sidebar-toggle' } },
      advance: 'click-target',
    },
    {
      id: 'filter-to-contracts',
      title: 'Select contracts stories',
      instruction: 'Check contracts under topic (or search Northstar in Filter) so only matching stories stay visible.',
      target: { kind: 'element', id: 'friendly-facet-topic' },
      expected: 'The grid narrows to just your filtered subset.',
      advance: 'next-button',
    },
    {
      id: 'open-views-tab',
      title: 'Open Views',
      instruction: 'Click Views to save this filtered slice so you can run actions on just these rows.',
      target: { kind: 'element', id: 'filter-sidebar-tab-views' },
      advance: 'click-target',
    },
    {
      id: 'save-contracts-view',
      title: 'Save the current View',
      instruction: 'Click Save current view, name it Contract stories, and save it.',
      target: { kind: 'element', id: 'create-view' },
      expected: 'Contract stories becomes your active saved View.',
      advance: 'next-button',
    },
    {
      id: 'open-analyze-ask',
      title: 'Run an action on just that View',
      instruction: 'Click Analyze while your saved View is active.',
      target: { kind: 'action-tab', id: 'analyze' },
      advance: 'click-target',
    },
    {
      id: 'open-ask-on-view',
      title: 'Choose Ask',
      instruction: 'Click Ask. Notice the Run footer automatically scopes to your active View instead of all 24 rows.',
      target: { kind: 'action', id: 'map.ask' },
      advance: 'click-target',
    },
    {
      id: 'configure-ask-on-view',
      title: 'Ask what needs verification',
      instruction: 'Ask what specific claim needs independent verification and what public record could verify it, saving to next_check (click Fill for me).',
      target: { kind: 'field', id: 'action-prompt' },
      fill: {
        kind: 'action-form',
        actionKind: 'map.ask',
        values: {
          source: ['story'],
          question: 'What specific claim in this story most needs independent verification, and what public record or source could verify it?',
        },
        outputNames: { answer: 'next_check' },
      },
      advance: 'next-button',
    },
    {
      id: 'run-ask-on-view',
      title: 'Run Ask on your View',
      instruction: 'Confirm the Run footer shows your active View scope, then click Run.',
      target: { kind: 'field', id: 'generated-action-run' },
      expected: 'Only rows inside your saved View are processed, saving time and model cost.',
      advance: 'click-target',
    },
    {
      id: 'open-analyze-judge',
      title: 'Grade outputs with Judge',
      instruction: 'Click Analyze to audit next_check with a second model pass.',
      target: { kind: 'completed-run', target: { kind: 'action-tab', id: 'analyze' } },
      advance: 'click-target',
    },
    {
      id: 'open-judge',
      title: 'Choose Judge results',
      instruction: 'Click Judge results. Unlike an answer key (Join expected answers), Judge grades open-ended AI outputs against your editorial rubric.',
      target: { kind: 'action', id: 'map.judge' },
      advance: 'click-target',
    },
    {
      id: 'configure-judge',
      title: 'Set your editorial rubric',
      instruction: 'Grade next_check against story using clear pass/fail editorial guidelines (click Fill for me).',
      target: { kind: 'field', id: 'field-guidelines' },
      fill: {
        kind: 'action-form',
        actionKind: 'map.judge',
        values: {
          source: ['story'],
          judged_column: 'next_check',
          guidelines: 'Pass only if the answer names one concrete claim from the story and identifies a specific public record, contract, email, or agency document that can verify it without inventing facts.',
        },
      },
      advance: 'next-button',
    },
    {
      id: 'run-judge',
      title: 'Run Judge results',
      instruction: 'Review the rubric and model, then click Run.',
      target: { kind: 'field', id: 'generated-action-run' },
      expected: 'verdict and judge_note populate beside next_check.',
      advance: 'click-target',
    },
    {
      id: 'review-judge-verdicts',
      title: 'Inspect Judge verdicts',
      instruction: 'Compare verdict and judge_note against next_check to spot weak or unsupported leads immediately.',
      target: { kind: 'completed-run', target: { kind: 'element', id: 'grid' } },
      expected: 'You have classified stories, scoped AI work to a View, and audited outputs against a rubric.',
      advance: 'next-button',
    },
  ],
};

export const COMBINE_VALUES_WALKTHROUGH: WalkthroughDefinition = {
  id: 'combine-values',
  title: 'Clean messy agencies & summarize spend',
  description: 'Auto-cluster formatting duplicates, combine editorial variants into four canonical agencies, and join clean spend to Contracts.',
  requirements: 'Runs locally with Cluster, Combine, and Join; no model provider is required.',
  steps: [
    {
      id: 'open-agency-payments',
      title: 'Open Agency payments',
      instruction: 'Click Agency payments. These 16 fictional payments came from exports that named four agencies in 12 inconsistent ways.',
      target: { kind: 'sheet', id: 'Agency payments' },
      expected: 'The raw payment records appear in Grid view.',
      advance: 'click-target',
    },
    {
      id: 'inspect-agency-column',
      title: 'Inspect the agency names',
      instruction: 'Read down agency. Notice abbreviations, capitalization differences, a city prefix, and leading or trailing spaces.',
      target: { kind: 'grid-column', id: 'agency' },
      expected: 'Four real agencies are fragmented across 12 exact values.',
      advance: 'next-button',
    },
    {
      id: 'open-raw-facets',
      title: 'Open Filter',
      instruction: 'Click Filter to count the raw spellings before cleaning.',
      target: { kind: 'element', id: 'filter-sidebar-toggle' },
      advance: 'click-target',
    },
    {
      id: 'choose-raw-agency-facet',
      title: 'See the 12 fragmented spellings',
      instruction: 'Inspect the agency facet. Public Works, Water, and Transit are split into separate buckets because case, spaces, abbreviations, and prefixes count as exact differences.',
      target: { kind: 'element', id: 'friendly-facet-agency' },
      expected: 'Filter reports 12 distinct values across 16 rows.',
      advance: 'next-button',
    },
    {
      id: 'open-transform-cluster',
      title: 'Open Transform',
      instruction: 'Click Transform and find the Resolve group for clustering and combining values.',
      target: { kind: 'action-tab', id: 'transform' },
      advance: 'click-target',
    },
    {
      id: 'open-cluster-values',
      title: 'Choose Cluster duplicate values',
      instruction: 'Click Cluster duplicate values to automatically catch case and spacing variants with fingerprint clustering.',
      target: { kind: 'action', id: 'cluster.values' },
      advance: 'click-target',
    },
    {
      id: 'configure-cluster',
      title: 'Choose agency and Fingerprint method',
      instruction: 'Select agency with Fingerprint clustering and output agency_canonical (click Fill for me).',
      target: { kind: 'field', id: 'cluster-column-select' },
      fill: {
        kind: 'action-form',
        actionKind: 'cluster.values',
        values: { source: 'agency', method: 'fingerprint' },
        outputNames: { canonical: 'agency_canonical' },
      },
      advance: 'next-button',
    },
    {
      id: 'preview-clusters',
      title: 'Preview automatic clusters',
      instruction: 'Click Preview groups to see the capitalization and whitespace variants Frisket grouped automatically.',
      target: { kind: 'element', id: 'cluster-preview-button' },
      expected: 'Three clusters appear immediately for Public Works, Transit, and Water.',
      advance: 'click-target',
    },
    {
      id: 'apply-clusters',
      title: 'Apply the automatic clusters',
      instruction: 'Review the suggested groups and click Apply groups to write agency_canonical.',
      target: { kind: 'field', id: 'cluster-commit-button' },
      expected: 'agency_canonical collapses case and whitespace noise from 12 spellings down to 7.',
      advance: 'click-target',
    },
    {
      id: 'open-transform-combine',
      title: 'Finish editorial variants in Transform',
      instruction: 'Click Transform. Automatic clustering caught formatting noise; abbreviations and city prefixes (Public Works Dept., Riverton Water Authority) need human review in Combine values.',
      target: { kind: 'action-tab', id: 'transform' },
      advance: 'click-target',
    },
    {
      id: 'open-combine',
      title: 'Choose Combine values',
      instruction: 'Click Combine values to bucket the remaining three editorial variants.',
      target: { kind: 'action', id: 'resolve.combine' },
      advance: 'click-target',
    },
    {
      id: 'choose-agency-canonical',
      title: 'Start from agency_canonical',
      instruction: 'Choose agency_canonical and name the output agency_clean (click Fill for me).',
      target: { kind: 'field', id: 'resolve-combine-column-select' },
      fill: {
        kind: 'action-form',
        actionKind: 'resolve.combine',
        values: { source: 'agency_canonical' },
        outputNames: { cleaned: 'agency_clean' },
      },
      advance: 'next-button',
    },
    {
      id: 'group-editorial-variants',
      title: 'Bucket the remaining 3 editorial variants',
      instruction: 'Select each pair and click New bucket: (Public Works Department + Public Works Dept.), (Water Authority + Riverton Water Authority), and (Transit Authority + Riverton Transit Authority). Leave Parks Department untouched.',
      target: { kind: 'element', id: 'resolve-combine-unassigned' },
      expected: 'Three reviewed buckets cover all six variants while Parks Department passes through unchanged.',
      advance: 'next-button',
    },
    {
      id: 'apply-agency-groups',
      title: 'Apply the reviewed buckets',
      instruction: 'Click Apply groups to write agency_clean without altering the raw agency column.',
      target: { kind: 'field', id: 'resolve-apply' },
      expected: 'agency_clean populates for all 16 payments.',
      advance: 'click-target',
    },
    {
      id: 'reopen-clean-facets',
      title: 'Return to Filter',
      instruction: 'Click Filter to verify the repaired agency counts.',
      target: { kind: 'element', id: 'filter-sidebar-toggle' },
      advance: 'click-target',
    },
    {
      id: 'choose-clean-agency-facet',
      title: 'Inspect agency_clean counts',
      instruction: 'Open the agency_clean facet in the Filter sidebar.',
      target: { kind: 'element', id: 'facet-header-agency_clean' },
      expected: 'The 12 raw spellings collapse cleanly to 4 agencies.',
      advance: 'click-target',
    },
    {
      id: 'verify-clean-counts',
      title: 'Verify the repaired totals',
      instruction: 'Confirm the final counts: Public Works Department 6, Water Authority 4, Transit Authority 4, and Parks Department 2.',
      target: { kind: 'element', id: 'facet-values-agency_clean' },
      advance: 'next-button',
    },
    {
      id: 'open-tables-spend',
      title: 'Connect clean spend to Contracts',
      instruction: 'Click Sheets to join cleaned agency payments to Contracts (you can also use Analyze → Summarize grouped rows to generate AI spend narratives per agency).',
      target: { kind: 'action-tab', id: 'tables' },
      advance: 'click-target',
    },
    {
      id: 'open-join-contracts',
      title: 'Choose Join sheets (by key)',
      instruction: 'Click Join sheets (by key) to bring contract ceilings beside each cleaned payment.',
      target: { kind: 'action', id: 'derive.join' },
      advance: 'click-target',
    },
    {
      id: 'configure-agency-join',
      title: 'Configure the Contracts join',
      instruction: 'Join to Contracts on vendor (left join) and name the sheet Agency spend with contracts (click Fill for me).',
      target: { kind: 'field', id: 'field-join_right_sheet' },
      fill: {
        kind: 'action-form',
        actionKind: 'derive.join',
        rightSheetName: 'Contracts',
        values: {
          join_keys: [{ left_column: 'vendor', right_column: 'vendor' }],
          how: 'left',
          indicator: false,
        },
        sheetName: 'Agency spend with contracts',
      },
      advance: 'next-button',
    },
    {
      id: 'run-agency-join',
      title: 'Create the joined spend sheet',
      instruction: 'Review the join settings, then click Join tables.',
      target: { kind: 'field', id: 'generated-action-run' },
      expected: 'Frisket opens Agency spend with contracts.',
      advance: 'click-target',
    },
    {
      id: 'review-agency-spend',
      title: 'Inspect clean agency spend',
      instruction: 'Compare agency_clean payments directly beside each vendor contract ceiling and status.',
      target: { kind: 'element', id: 'grid' },
      expected: 'Four canonical agencies now line up cleanly with municipal contract records.',
      advance: 'next-button',
    },
  ],
};

export const COUNCIL_AUDIO_WALKTHROUGH: WalkthroughDefinition = {
  id: 'council-audio',
  title: 'Transcribe & search meeting audio',
  description: 'Play local meeting excerpts, transcribe them with timestamps, and find grounded commitments with clickable citations.',
  requirements: 'Requires an available speech-to-text engine and model provider.',
  steps: [
    {
      id: 'open-council-audio',
      title: 'Open the meeting audio',
      instruction: 'Click Council audio. Both rows are rights-cleared local five-minute municipal meeting excerpts.',
      target: { kind: 'sheet', id: 'Council audio' },
      advance: 'click-target',
    },
    {
      id: 'open-audio-document-view',
      title: 'Switch to Document view',
      instruction: 'Click Document to listen to the recordings directly inside Frisket.',
      target: { kind: 'view', id: 'document' },
      advance: 'click-target',
    },
    {
      id: 'play-audio',
      title: 'Listen to an excerpt',
      instruction: 'Play part of the selected recording. One meeting discusses policy and lobbying; the other discusses police oversight.',
      target: { kind: 'element', id: 'document-audio' },
      expected: 'The audio plays locally without relying on a remote host.',
      advance: 'next-button',
    },
    {
      id: 'open-audio-read',
      title: 'Open Media',
      instruction: 'Click Media and find the Transcripts group.',
      target: { kind: 'action-tab', id: 'media' },
      advance: 'click-target',
    },
    {
      id: 'open-transcribe',
      title: 'Choose Transcribe',
      instruction: 'Click Transcribe to create searchable, time-aligned text.',
      target: { kind: 'action', id: 'media.transcribe' },
      advance: 'click-target',
    },
    {
      id: 'configure-transcribe',
      title: 'Choose the audio column and outputs',
      instruction: 'Select media and name the outputs transcript and transcript_segments (click Fill for me).',
      target: { kind: 'field', id: 'field-source' },
      fill: {
        kind: 'action-form',
        actionKind: 'media.transcribe',
        values: { source: 'media' },
        outputNames: { text: 'transcript', segments: 'transcript_segments' },
      },
      advance: 'next-button',
    },
    {
      id: 'choose-transcription-engine',
      title: 'Choose a transcription engine',
      instruction: 'Confirm an available local, sidecar, or remote speech-to-text engine.',
      target: { kind: 'tour', id: 'action-engine-selector' },
      advance: 'next-button',
    },
    {
      id: 'review-transcripts-grid',
      title: 'Return to Grid before running',
      instruction: 'Click Grid so you can see transcript and transcript_segments populate.',
      target: { kind: 'view', id: 'grid' },
      advance: 'click-target',
    },
    {
      id: 'run-transcription',
      title: 'Transcribe both excerpts',
      instruction: 'Click Run and let transcription finish.',
      target: { kind: 'field', id: 'generated-action-run' },
      expected: 'Transcript text and timestamped segments populate for each recording.',
      advance: 'click-target',
    },
    {
      id: 'open-timed-transcript',
      title: 'Open the timed transcript',
      instruction: 'Click Document. The recording and its timestamped transcript now share one synchronized surface.',
      target: { kind: 'completed-run', target: { kind: 'view', id: 'document' } },
      advance: 'click-target',
    },
    {
      id: 'review-timed-transcript',
      title: 'Click any line to seek audio',
      instruction: 'Click a timestamped phrase to jump playback straight to that moment and verify quotes or names by ear.',
      target: { kind: 'element', id: 'document-temporal-transcript' },
      expected: 'The player seeks immediately while the active phrase tracks playback.',
      advance: 'next-button',
    },
    {
      id: 'return-audio-grid',
      title: 'Return to Grid',
      instruction: 'Click Grid before scanning the transcripts for commitments.',
      target: { kind: 'view', id: 'grid' },
      advance: 'click-target',
    },
    {
      id: 'open-audio-analyze',
      title: 'Open Analyze',
      instruction: 'Click Analyze to scan long transcripts for concrete commitments with citations.',
      target: { kind: 'action-tab', id: 'analyze' },
      advance: 'click-target',
    },
    {
      id: 'open-audio-find',
      title: 'Choose Find grounded occurrences',
      instruction: 'Click Find grounded occurrences. Unlike a high-level summary, Find scans every window and returns a sheet of exact, cited findings.',
      target: { kind: 'action', id: 'map.find' },
      advance: 'click-target',
    },
    {
      id: 'configure-audio-find',
      title: 'Describe what to find',
      instruction: 'Search transcript for budget commitments, oversight requests, or policy deadlines, saving to Council commitments (click Fill for me).',
      target: { kind: 'field', id: 'field-instruction' },
      fill: {
        kind: 'action-form',
        actionKind: 'map.find',
        values: {
          source: 'transcript',
          instruction: 'Find every budget commitment, oversight request, or policy deadline mentioned by speakers.',
        },
        sheetName: 'Council commitments',
      },
      advance: 'next-button',
    },
    {
      id: 'run-audio-find',
      title: 'Find grounded commitments',
      instruction: 'Confirm the model, then click Run.',
      target: { kind: 'field', id: 'generated-action-run' },
      expected: 'A Council commitments sheet populates with cited excerpts linked back to each recording.',
      advance: 'click-target',
    },
    {
      id: 'finish-audio-review',
      title: 'Verify cited commitments',
      instruction: 'Inspect the extracted commitments and follow their citations back to the timed transcript to verify exact speaker wording.',
      target: { kind: 'completed-run', target: { kind: 'element', id: 'grid' } },
      expected: 'Every finding stays tied to its verbatim transcript span and audio timestamp.',
      advance: 'next-button',
    },
  ],
};

export const RSS_IMPORT_WALKTHROUGH: WalkthroughDefinition = {
  id: 'rss-import',
  title: 'Monitor a live feed with an Alert',
  description: 'Connect the public Riverton RSS feed, save a focused View, and attach an Alert so matching stories surface automatically.',
  requirements: 'Requires network access to frisket-dev.github.io.',
  steps: [
    {
      id: 'open-import-tab',
      title: 'Open Data',
      instruction: 'Click Data to see the project import and feed tools.',
      target: { kind: 'action-tab', id: 'data' },
      advance: 'click-target',
    },
    {
      id: 'open-import-workspace',
      title: 'Choose Import data',
      instruction: 'Click Import data to open the import workspace.',
      target: { kind: 'command', id: 'import' },
      advance: 'click-target',
    },
    {
      id: 'choose-feed-mode',
      title: 'Choose Feed',
      instruction: 'Click Feed in the import sidebar.',
      target: { kind: 'element', id: 'import-mode-feed' },
      advance: 'click-target',
    },
    {
      id: 'fill-feed-details',
      title: 'Enter the Riverton RSS feed',
      instruction: {
        lead: 'Enter',
        code: 'https://frisket-dev.github.io/frisket-samples/riverton/feed.xml',
        explanation: 'Name it Riverton Dispatch RSS (or click Fill for me).',
      },
      target: { kind: 'element', id: 'feed-url' },
      fill: {
        kind: 'feed-import',
        url: 'https://frisket-dev.github.io/frisket-samples/riverton/feed.xml',
        name: 'Riverton Dispatch RSS',
        feedKind: 'rss',
      },
      advance: 'next-button',
    },
    {
      id: 'add-feed',
      title: 'Add the feed',
      instruction: 'Review the feed URL and name, then click Add feed.',
      target: { kind: 'element', id: 'feed-create' },
      expected: 'Frisket saves the feed and offers to populate it now.',
      advance: 'click-target',
    },
    {
      id: 'populate-feed',
      title: 'Fetch current items',
      instruction: 'Click Import to pull the latest items from the RSS feed.',
      target: { kind: 'element', id: 'feed-populate-run' },
      expected: 'A live feed sheet is created with the current Riverton items.',
      advance: 'click-target',
    },
    {
      id: 'review-feed',
      title: 'Review the imported feed',
      instruction: 'Inspect the imported title, summary, and publication dates. Next, turn a filter into a live Alert.',
      target: { kind: 'element', id: 'grid' },
      advance: 'next-button',
    },
    {
      id: 'open-feed-filter',
      title: 'Filter for priority stories',
      instruction: 'Click Filter and search for contract (or emergency) to narrow the feed to stories you want to monitor.',
      target: { kind: 'element', id: 'filter-sidebar-toggle' },
      advance: 'click-target',
    },
    {
      id: 'open-feed-views',
      title: 'Open Views',
      instruction: 'Click Views to save your current search as a reusable View.',
      target: { kind: 'element', id: 'filter-sidebar-tab-views' },
      advance: 'click-target',
    },
    {
      id: 'save-feed-view',
      title: 'Save your alert View',
      instruction: 'Click Save current view, name it Contract dispatches, and click Save view.',
      target: { kind: 'element', id: 'create-view' },
      expected: 'Contract dispatches is saved and ready to power an Alert.',
      advance: 'next-button',
    },
    {
      id: 'open-feed-alerts',
      title: 'Open Alerts',
      instruction: 'Click Alerts to attach continuous monitoring to your saved View.',
      target: { kind: 'discover-tab', id: 'Alerts' },
      advance: 'click-target',
    },
    {
      id: 'create-feed-alert',
      title: 'Create an Alert on the View',
      instruction: 'Click New alert from current view, name it New contract dispatches, and save. Whenever future RSS checks match this View, Frisket surfaces them automatically.',
      target: { kind: 'element', id: 'new-alert-from-current-grid' },
      expected: 'You now have an end-to-end live Feed → View → Alert monitoring loop.',
      advance: 'next-button',
    },
  ],
};

export const MULTILINGUAL_NAMES_WALKTHROUGH: WalkthroughDefinition = {
  id: 'multilingual-names',
  title: 'Standardize a foreign registry',
  description: 'Translate foreign-language registry notes, transliterate five scripts to Latin, and cluster duplicates across three distinct entities.',
  requirements: 'Translate uses an available translation engine or model; Transliterate and Cluster run locally.',
  steps: [
    {
      id: 'open-foreign-registry',
      title: 'Open Foreign registry',
      instruction: 'Click Foreign registry. These 12 records span three distinct entities—Alexander Petrov, Elena Morozova, and Harbor Maritime Holdings—across five scripts with Russian, French, Spanish, and German notes.',
      target: { kind: 'sheet', id: 'Foreign registry' },
      expected: 'The raw multilingual registry appears in Grid view.',
      advance: 'click-target',
    },
    {
      id: 'inspect-foreign-columns',
      title: 'Inspect names and foreign notes',
      instruction: 'Scan name_as_written and record_note. Keeping both raw columns preserves source fidelity while you translate notes and standardize entity names.',
      target: { kind: 'grid-column', id: 'name_as_written' },
      advance: 'next-button',
    },
    {
      id: 'open-transform-translate',
      title: 'Open Transform',
      instruction: 'Click Transform and find the Language group.',
      target: { kind: 'action-tab', id: 'transform' },
      advance: 'click-target',
    },
    {
      id: 'open-translate',
      title: 'Choose Translate',
      instruction: 'Click Translate to turn the Russian, French, Spanish, and German record_note entries into English.',
      target: { kind: 'action', id: 'map.translate' },
      advance: 'click-target',
    },
    {
      id: 'configure-translate',
      title: 'Translate record_note to English',
      instruction: 'Choose record_note, translate to English, and name the output record_note_en (click Fill for me).',
      target: { kind: 'field', id: 'text-source-columns' },
      fill: {
        kind: 'action-form',
        actionKind: 'map.translate',
        values: { source: ['record_note'], target_language: 'English' },
        outputNames: { translation: 'record_note_en' },
      },
      advance: 'next-button',
    },
    {
      id: 'run-translate',
      title: 'Run Translate',
      instruction: 'Confirm the translation engine or model, then click Run.',
      target: { kind: 'field', id: 'generated-action-run' },
      expected: 'Every foreign-language note receives an English translation in record_note_en.',
      advance: 'click-target',
    },
    {
      id: 'open-transform-transliterate',
      title: 'Return to Transform for scripts',
      instruction: 'Click Transform. Translation converts meaning; next we will transliterate personal and company names into Latin script locally.',
      target: { kind: 'completed-run', target: { kind: 'action-tab', id: 'transform' } },
      advance: 'click-target',
    },
    {
      id: 'open-transliterate',
      title: 'Choose Transliterate to Latin script',
      instruction: 'Click Transliterate to Latin script. Transliteration changes scripts locally without merging distinct people.',
      target: { kind: 'action', id: 'frisket.transliterate.transliterate' },
      advance: 'click-target',
    },
    {
      id: 'configure-transliterate',
      title: 'Choose name_as_written',
      instruction: 'Select name_as_written as the text column (click Fill for me).',
      target: { kind: 'field', id: 'field-text' },
      fill: {
        kind: 'action-form',
        actionKind: 'frisket.transliterate.transliterate',
        values: { text: 'name_as_written' },
      },
      advance: 'next-button',
    },
    {
      id: 'run-transliterate',
      title: 'Transliterate every name',
      instruction: 'Click Run to convert Cyrillic, Arabic, Greek, and Katakana names to Latin script locally.',
      target: { kind: 'field', id: 'generated-action-run' },
      expected: 'latin_text populates for all 12 rows.',
      advance: 'click-target',
    },
    {
      id: 'review-transliteration',
      title: 'Review the Latin-script names',
      instruction: 'Compare latin_text with name_as_written. All three entities are now readable in Latin script, while script variants and casing differences remain visible.',
      target: { kind: 'completed-run', target: { kind: 'grid-column', id: 'latin_text' } },
      advance: 'next-button',
    },
    {
      id: 'open-transform-cluster-names',
      title: 'Cluster duplicates without over-merging',
      instruction: 'Click Transform and open the Resolve group.',
      target: { kind: 'action-tab', id: 'transform' },
      advance: 'click-target',
    },
    {
      id: 'open-cluster-names',
      title: 'Choose Cluster duplicate values',
      instruction: 'Click Cluster duplicate values to group clear casing duplicates while keeping Alexander Petrov, Elena Morozova, and Harbor Maritime Holdings distinct.',
      target: { kind: 'action', id: 'cluster.values' },
      advance: 'click-target',
    },
    {
      id: 'configure-cluster-names',
      title: 'Cluster latin_text with Fingerprint',
      instruction: 'Choose latin_text with Fingerprint clustering and save to entity_canonical (click Fill for me).',
      target: { kind: 'field', id: 'cluster-column-select' },
      fill: {
        kind: 'action-form',
        actionKind: 'cluster.values',
        values: { source: 'latin_text', method: 'fingerprint' },
        outputNames: { canonical: 'entity_canonical' },
      },
      advance: 'next-button',
    },
    {
      id: 'preview-name-clusters',
      title: 'Preview the entity clusters',
      instruction: 'Click Preview groups. Notice it merges obvious duplicates without falsely collapsing distinct people into one.',
      target: { kind: 'element', id: 'cluster-preview-button' },
      advance: 'click-target',
    },
    {
      id: 'apply-name-clusters',
      title: 'Apply the reviewed clusters',
      instruction: 'Click Apply groups to write entity_canonical.',
      target: { kind: 'field', id: 'cluster-commit-button' },
      advance: 'click-target',
    },
    {
      id: 'review-standardized-registry',
      title: 'Compare raw, translated, and canonical values',
      instruction: 'Inspect name_as_written, latin_text, entity_canonical, and record_note_en side by side. For cross-script phonetic variants (Arabic or Katakana), follow up with Substitute values under human judgment.',
      target: { kind: 'element', id: 'grid' },
      expected: 'You preserved original records while making three distinct foreign-registry entities searchable and comparable.',
      advance: 'next-button',
    },
  ],
};

export const REGEX_EXTRACT_WALKTHROUGH: WalkthroughDefinition = {
  id: 'regex-extract',
  title: 'Follow contracts across sheets',
  description: 'Extract contract IDs from stories with regex, join them to Contracts, and inspect unmatched leads.',
  steps: [
    {
      id: 'open-dispatches',
      title: 'Open Dispatches',
      instruction: 'Click Dispatches so the extraction starts from the story sheet.',
      target: { kind: 'sheet', id: 'Dispatches' },
      expected: 'The 24 civic stories appear.',
      advance: 'click-target',
    },
    {
      id: 'open-dispatches-grid',
      title: 'Start in Grid',
      instruction: 'Click Grid so new columns will be visible as they populate.',
      target: { kind: 'view', id: 'grid' },
      advance: 'click-target',
    },
    {
      id: 'inspect-story-column',
      title: 'Read the source stories',
      instruction: 'Look down the story column and notice recurring contract IDs like CTR-2026-014 inside the reporting text.',
      target: { kind: 'grid-column', id: 'story' },
      expected: 'You can see repeatable contract IDs ready for deterministic regex extraction.',
      advance: 'next-button',
    },
    {
      id: 'open-extract',
      title: 'Open Analyze',
      instruction: 'Click Analyze and find the Extract group of actions.',
      target: { kind: 'action-tab', id: 'analyze' },
      expected: 'The Analyze and Extract action groups appear.',
      advance: 'click-target',
    },
    {
      id: 'open-regex',
      title: 'Choose Extract text with regex',
      instruction: 'Click Extract text with regex. This action extracts repeatable patterns locally with zero model calls.',
      target: { kind: 'action', id: 'map.regex_extract' },
      expected: 'The Extract text with regex form opens.',
      advance: 'click-target',
    },
    {
      id: 'configure-regex',
      title: 'Configure the contract pattern',
      instruction: {
        lead: 'Search story with',
        code: '\\bCTR-\\d{4}-\\d{3}\\b',
        explanation: 'This regular expression matches CTR-(four digits)-(three digits) and saves to contract_id. Click Fill for me to populate the form.',
        learnMore: {
          label: 'Learn more about regular expressions',
          href: 'https://docs.python.org/3/howto/regex.html',
        },
      },
      target: { kind: 'field', id: 'field-pattern' },
      fill: {
        kind: 'action-form',
        actionKind: 'map.regex_extract',
        values: { input_columns: ['story'], pattern: '\\bCTR-\\d{4}-\\d{3}\\b' },
        outputNames: { extracted: 'contract_id' },
      },
      expected: 'Source, pattern, and output name are filled and ready to review.',
      advance: 'next-button',
    },
    {
      id: 'run-action',
      title: 'Run the extraction',
      instruction: 'Review the filled pattern and output column, then click Run.',
      target: { kind: 'field', id: 'generated-action-run' },
      expected: 'The action starts and the form closes.',
      advance: 'click-target',
    },
    {
      id: 'review-results',
      title: 'Review the extracted IDs',
      instruction: 'Look across the grid for the new contract_id column as its values populate.',
      target: { kind: 'completed-run', target: { kind: 'element', id: 'grid' } },
      expected: 'Stories with a contract reference receive an extracted ID; other rows stay empty.',
      advance: 'next-button',
    },
    {
      id: 'open-analyze',
      title: 'Open Sheets',
      instruction: 'Click Sheets to find actions that reshape and join sheets.',
      target: { kind: 'action-tab', id: 'tables' },
      expected: 'The Sheets actions appear.',
      advance: 'click-target',
    },
    {
      id: 'open-join',
      title: 'Choose Join sheets',
      instruction: 'Click Join sheets (by key). This creates a new sheet from matching contract IDs.',
      target: { kind: 'action', id: 'derive.join' },
      expected: 'The deterministic join form opens.',
      advance: 'click-target',
    },
    {
      id: 'configure-join',
      title: 'Join Dispatches to Contracts',
      instruction: {
        lead: 'Join to Contracts on contract_id (outer join with indicator) and name the sheet',
        code: 'Contract trail',
        explanation: 'Click Fill for me to configure the right sheet, key pair, outer join, indicator, and sheet name.',
      },
      target: { kind: 'field', id: 'field-join_right_sheet' },
      fill: {
        kind: 'action-form',
        actionKind: 'derive.join',
        rightSheetName: 'Contracts',
        values: {
          join_keys: [{ left_column: 'contract_id', right_column: 'contract_id' }],
          how: 'outer',
          indicator: true,
        },
        sheetName: 'Contract trail',
      },
      advance: 'next-button',
    },
    {
      id: 'run-join',
      title: 'Create the joined sheet',
      instruction: 'Review the join settings, then click Join tables.',
      target: { kind: 'field', id: 'generated-action-run' },
      expected: 'Frisket opens the new Contract trail sheet.',
      advance: 'click-target',
    },
    {
      id: 'review-join',
      title: 'Inspect the contract trail',
      instruction: 'Use the indicator column to find story-only and contract-only rows, then inspect the matched vendor and agency details.',
      target: { kind: 'element', id: 'grid' },
      expected: 'The result preserves matches as well as leads that need follow-up.',
      advance: 'next-button',
    },
  ],
};

export const LOCAL_MODEL_LAB_ENDPOINT_NAME = 'Local model lab server';

export const LOCAL_MODEL_LAB_WALKTHROUGH: WalkthroughDefinition = {
  id: 'local-model-lab',
  title: 'Learn what a small local model can — and cannot — do',
  description: 'Connect a named model server, run a dependable labeling task, then compare a harder reasoning task with an answer key.',
  requirements: 'Requires Ollama and an AI call mode that permits live requests. The recommended qwen3:0.6b model is a 523 MB download.',
  editions: ['local', 'team'],
  steps: [
    {
      id: 'open-local-model-lab',
      title: 'Open Small model lab',
      instruction: 'Click Small model lab. It contains easy notices, harder reasoning puzzles, and editor-written answers for comparison.',
      target: { kind: 'sheet', id: 'Small model lab' },
      expected: 'Six synthetic records appear in Grid view.',
      advance: 'click-target',
    },
    {
      id: 'inspect-easy-notices',
      title: 'Start with the easy notices',
      instruction: 'Read easy_notice and expected_notice_type. Each short notice clearly identifies itself as a meeting, inspection, or contract.',
      target: { kind: 'grid-column', id: 'easy_notice' },
      expected: 'The answer is explicit enough for a very small model.',
      advance: 'next-button',
    },
    {
      id: 'open-local-account-menu',
      title: 'Open the account menu',
      instruction: 'Open the account menu so you can configure the local Ollama server before choosing a model.',
      target: { kind: 'element', id: 'chrome-account' },
      advance: 'click-target',
    },
    {
      id: 'open-local-account-settings',
      title: 'Open account settings',
      instruction: 'Click Account settings. Frisket remembers this project so you can return after setup.',
      target: { kind: 'element', id: 'account-settings' },
      advance: 'click-target',
    },
    {
      id: 'configure-local-providers',
      title: 'Open AI providers',
      instruction: 'Choose AI providers to add and probe a named model server.',
      target: { kind: 'element', id: 'settings-nav-personal-ai-providers' },
      advance: 'click-target',
    },
    {
      id: 'open-local-server-form',
      title: 'Add a model server',
      instruction: 'Click Add model server. Every server gets its own stable identity, even when two machines offer the same model name.',
      target: { kind: 'element', id: 'local-endpoint-add' },
      advance: 'click-target',
    },
    {
      id: 'name-local-server',
      title: 'Name this server',
      instruction: { lead: 'Set Name to', code: LOCAL_MODEL_LAB_ENDPOINT_NAME },
      target: { kind: 'field', id: 'local-endpoint-add-name' },
      advance: 'next-button',
    },
    {
      id: 'point-at-local-server',
      title: 'Enter the server URL',
      instruction: {
        lead: 'Install and start Ollama on the machine that will run the model, then set URL to',
        code: 'http://localhost:11434',
        explanation: 'If Ollama runs on another machine, enter that reachable origin instead.',
        learnMore: { label: 'Download Ollama', href: 'https://ollama.com/download' },
      },
      target: { kind: 'field', id: 'local-endpoint-add-origin' },
      advance: 'next-button',
    },
    {
      id: 'save-local-server',
      title: 'Save this endpoint',
      instruction: 'Click Add server. Frisket assigns this origin an immutable endpoint ID so future runs keep routing to the machine you selected.',
      target: { kind: 'field', id: 'local-endpoint-add-submit' },
      expected: 'A Local model lab server row appears with its own reachability, authentication, and download controls.',
      advance: 'click-target',
    },
    {
      id: 'verify-local-reachability',
      title: 'Check the server status',
      instruction: 'Confirm the new endpoint row says reachable. If it does not, check where Ollama is running; changing its URL means creating a new endpoint.',
      target: {
        kind: 'local-endpoint',
        label: LOCAL_MODEL_LAB_ENDPOINT_NAME,
        part: 'row',
      },
      advance: 'next-button',
    },
    {
      id: 'install-small-model',
      title: 'Install the small model',
      instruction: {
        lead: 'If qwen3:0.6b is not listed, enable in-app downloads and use the Download button, or run',
        code: 'ollama pull qwen3:0.6b',
        explanation: 'The in-app option is deliberately opt-in because it writes hundreds of megabytes to the model-server machine.',
        learnMore: { label: 'View the model in Ollama', href: 'https://ollama.com/library/qwen3:0.6b' },
      },
      target: {
        kind: 'local-endpoint',
        label: LOCAL_MODEL_LAB_ENDPOINT_NAME,
        part: 'guidance',
      },
      expected: 'qwen3:0.6b appears under Local model lab server after the download and a Recheck.',
      advance: 'next-button',
    },
    {
      id: 'return-to-local-project',
      title: 'Return to the project',
      instruction: 'Return to the sample project. Settings are saved for this workspace.',
      target: { kind: 'element', id: 'settings-return-project' },
      advance: 'click-target',
    },
    {
      id: 'reopen-local-analyze',
      title: 'Open Analyze',
      instruction: 'Click Analyze to configure the easy labeling task.',
      target: { kind: 'action-tab', id: 'analyze' },
      advance: 'click-target',
    },
    {
      id: 'reopen-local-classify',
      title: 'Choose Classify',
      instruction: 'Click Classify.',
      target: { kind: 'action', id: 'map.classify' },
      advance: 'click-target',
    },
    {
      id: 'configure-easy-classify',
      title: 'Fill the easy classification task',
      instruction: {
        lead: 'Classify easy_notice with labels',
        code: 'meeting, inspection, contract',
        explanation: 'Save to small_model_label (click Fill for me to populate source, labels, and output name).',
      },
      target: { kind: 'field', id: 'classify-labels' },
      fill: {
        kind: 'action-form',
        actionKind: 'map.classify',
        values: {
          source: ['easy_notice'],
          fields: [
            {
              name: 'category',
              type: 'category',
              labels: ['meeting', 'inspection', 'contract'],
              description: '',
            },
          ],
        },
        outputNames: { category: 'small_model_label' },
      },
      expected: 'The category schema limits the model to the three known answers.',
      advance: 'next-button',
    },
    {
      id: 'choose-small-model',
      title: 'Choose qwen3:0.6b',
      instruction: 'Open the model picker and choose qwen3:0.6b under Local model lab server. The saved model ID includes that endpoint, so an identical tag on another server stays distinct.',
      target: { kind: 'tour', id: 'action-engine-selector' },
      advance: 'next-button',
    },
    {
      id: 'run-easy-local-task',
      title: 'Run the easy task locally',
      instruction: 'Click Run. The endpoint-qualified local model has no provider charge in Frisket; access to the machine itself is controlled by its operator.',
      target: { kind: 'field', id: 'generated-action-run' },
      expected: 'small_model_label populates for all six rows.',
      advance: 'click-target',
    },
    {
      id: 'review-easy-local-task',
      title: 'Compare with the easy answer key',
      instruction: 'Compare small_model_label with expected_notice_type. This constrained task should be a comfortable fit for the tiny model.',
      target: { kind: 'completed-run', target: { kind: 'grid-column', id: 'small_model_label' } },
      expected: 'The generated labels match, or any mistake is immediately visible.',
      advance: 'next-button',
    },
    {
      id: 'open-hard-analyze',
      title: 'Return to Analyze',
      instruction: 'Click Analyze for a task that requires tracking several rules and decoys.',
      target: { kind: 'action-tab', id: 'analyze' },
      advance: 'click-target',
    },
    {
      id: 'open-hard-ask',
      title: 'Choose Ask',
      instruction: 'Click Ask to pose the same reasoning question to every challenge.',
      target: { kind: 'action', id: 'map.ask' },
      advance: 'click-target',
    },
    {
      id: 'configure-hard-ask',
      title: 'Fill the harder reasoning prompt',
      instruction: {
        lead: 'Ask challenge_text:',
        code: 'Apply every rule in the text. Return the single requested ID or result, followed by one sentence explaining it.',
        explanation: 'Save to small_model_answer (click Fill for me to populate source, prompt, and output name).',
      },
      target: { kind: 'field', id: 'action-prompt' },
      fill: {
        kind: 'action-form',
        actionKind: 'map.ask',
        values: {
          source: ['challenge_text'],
          question: 'Apply every rule in the text. Return the single requested ID or result, followed by one sentence explaining it.',
        },
        outputNames: { answer: 'small_model_answer' },
      },
      advance: 'next-button',
    },
    {
      id: 'confirm-hard-model',
      title: 'Choose the same small model',
      instruction: 'Choose qwen3:0.6b under Local model lab server again. Actions keep separate drafts, so make the endpoint explicit before comparing results.',
      target: { kind: 'tour', id: 'action-engine-selector' },
      advance: 'next-button',
    },
    {
      id: 'run-hard-local-task',
      title: 'Run the harder task',
      instruction: 'Click Run. This local run starts without an unknown-provider-cost dialog; the answer key is deliberately not included in the model input.',
      target: { kind: 'field', id: 'generated-action-run' },
      expected: 'small_model_answer populates beside the authored challenge_answer.',
      advance: 'click-target',
    },
    {
      id: 'review-local-limit',
      title: 'See the capability boundary',
      instruction: 'Compare small_model_answer with challenge_answer one row at a time. Count exact matches, inspect confident mistakes, and decide whether this task needs a larger local model, a cloud model, or a deterministic rule.',
      target: { kind: 'completed-run', target: { kind: 'grid-column', id: 'small_model_answer' } },
      expected: 'The easy task and hard task now have visible, reviewable results instead of a vague impression that the model worked.',
      advance: 'next-button',
    },
  ],
};

export const WALKTHROUGHS = [
  PROJECT_ASK_WALKTHROUGH,
  LAWSUIT_DOCUMENT_WALKTHROUGH,
  CIVIC_AI_TRIAGE_WALKTHROUGH,
  COMBINE_VALUES_WALKTHROUGH,
  COUNCIL_AUDIO_WALKTHROUGH,
  RSS_IMPORT_WALKTHROUGH,
  MULTILINGUAL_NAMES_WALKTHROUGH,
  REGEX_EXTRACT_WALKTHROUGH,
  LOCAL_MODEL_LAB_WALKTHROUGH,
] as const;
