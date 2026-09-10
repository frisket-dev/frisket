// Decides whether a just-finished import should offer to extract entities.
// Pure: no React, no api, so the decision itself is unit-testable without a
// DOM.
//
// The refusal is not persisted, matching the sibling "Download media?" and
// "Populate feed?" prompts in this dialog: an import creates a new sheet, so
// this fires once per sheet by construction and there is no second time to
// suppress.

import { compatibleSourceColumns, sourceRequirementForParam } from '../actions/model';
import type { ActionTemplate, ColumnDef } from '../api/open';

/** The engine the drawer will default to, and therefore the one whose cost
 *  posture this prompt is allowed to describe. */
const PROMPT_ENGINE_ID = 'spacy';

export interface NerImportOffer {
  /** Ordered names of the columns a run would read, for the copy. */
  columnNames: string[];
  rowCount: number;
  /** True only when the default engine is known to run locally at no
   *  per-call cost. Anything else and the prompt stays silent about money
   *  rather than implying free. */
  free: boolean;
}

/** The offer, or null when this import should say nothing.
 *
 *  Keyed on "the import produced a column a run could read", not on the file
 *  being a PDF: OCR'd images, DOCX and transcripts all produce the same
 *  thing, and an extension list would silently miss formats. */
export function nerImportOffer({
  columns,
  rowCount,
  nerTemplate,
}: {
  columns: ColumnDef[];
  rowCount: number;
  nerTemplate: ActionTemplate | undefined;
}): NerImportOffer | null {
  if (!nerTemplate || rowCount <= 0) return null;

  // Already extracted: the work is done, so there is nothing to offer.
  if (columns.some((column) => column.semanticType === 'entity_mentions')) {
    return null;
  }

  const engine = nerTemplate.engines?.find(
    (candidate) => candidate.id === PROMPT_ENGINE_ID,
  );
  // Only suppress on a KNOWN-unavailable engine. Offering something that can
  // only fail on an install hint is worse than not offering; but an absent
  // catalog (engines undefined) is not evidence of absence.
  if (engine && engine.available === false) return null;

  const requirement = sourceRequirementForParam(nerTemplate, 'input_columns');
  const readable = compatibleSourceColumns(columns, requirement);
  if (readable.length === 0) return null;

  return {
    columnNames: readable.map((column) => column.name),
    rowCount,
    free: engine?.tier === 'local' && engine?.billable !== true,
  };
}

/** "body", "body and notes", "body, notes and 2 more". */
export function describeColumns(names: string[]): string {
  if (names.length === 1) return names[0];
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names[0]}, ${names[1]} and ${names.length - 2} more`;
}
