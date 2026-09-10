// The `map.ner` label model: spaCy's CANONICAL entity types, their tiers, and
// the parse/serialize rules for the comma-joined `labels` param. A pure .ts
// module (the outputFieldModel.ts/runLifecycleState.ts precedent in this
// directory) so the chip view (NerFieldControls.tsx), the orchestrator's
// validation gate (ActionForm.tsx), and the tests share ONE table — and so
// react-refresh/only-export-components stays satisfied in the .tsx view.
//
// The chips used to
// render the 18 RAW OntoNotes tags. The request side canonicalizes what you
// type while the emit side compares against the CANONICAL type, so the `GPE`
// chip asked for `gpe` and every span emitted `location` — a silent zero.
// Rendering the 17 canonical types instead removes the round-trip asymmetry
// entirely (the python side canonicalizes request and emit identically), stops
// offering two chips (`GPE`/`LOC`) for one concept, and spells out the two
// unguessable acronyms (NORP, FAC).
import { parseCommaLabels } from './formControlHelpers';
import { ENTITY_TYPE_ALIASES } from '../../generated/actionTypes';

export interface SpacyTypeOption {
  /** The CANONICAL value written into `labels` — never the raw OntoNotes tag. */
  value: string;
  /** The PLURAL name, for the form's checkbox chips and the Mentions panel's
   *  section headings ("Organizations", "People") — both describe a SET. */
  label: string;
  /** The SINGULAR name, for anything describing ONE mention: a filter chip, the
   *  mention-detail panel's header. Reusing `label` there reads worse than the
   *  raw type for every common case ("Ada Lovelace · People"), which is why
   *  this is a second column in the same table rather than a derivation. */
  singular: string;
}

export type SpacyTypeTierId = 'recommended' | 'standard' | 'noise';

export interface SpacyTypeTier {
  id: SpacyTypeTierId;
  /** Visible caption. The noise tier is distinguished, never hidden: an
   *  Advanced disclosure would reintroduce "you can't see what you asked
   *  for", which is the failure class this whole change removes. */
  caption: string;
  options: SpacyTypeOption[];
}

/** D10's who / where / when / how-much set — checked on a NEW run. A visible,
 *  editable seed, never a fallback: an empty selection is a validation error
 *  (C4b), not a hidden substitution. Mirrors `RECOMMENDED_NER_LABELS`
 *  (src/frisket/contracts/actions/schemas/maps.py). */
export const RECOMMENDED_SPACY_TYPES = [
  'person',
  'organization',
  'location',
  'date',
  'money',
] as const;

export const SPACY_TYPE_TIERS: SpacyTypeTier[] = [
  {
    id: 'recommended',
    caption: 'Recommended',
    options: [
      { value: 'person', label: 'People', singular: 'Person' },
      { value: 'organization', label: 'Organizations', singular: 'Organization' },
      { value: 'location', label: 'Places', singular: 'Place' },
      { value: 'date', label: 'Dates', singular: 'Date' },
      { value: 'money', label: 'Money', singular: 'Money' },
    ],
  },
  {
    id: 'standard',
    caption: 'Also available',
    options: [
      { value: 'product', label: 'Products', singular: 'Product' },
      { value: 'event', label: 'Events', singular: 'Event' },
      { value: 'law', label: 'Laws', singular: 'Law' },
      { value: 'work_of_art', label: 'Works of art', singular: 'Work of art' },
      { value: 'language', label: 'Languages', singular: 'Language' },
      { value: 'norp', label: 'Nationalities, religious & political groups', singular: 'Nationality, religious or political group' },
      { value: 'fac', label: 'Facilities', singular: 'Facility' },
    ],
  },
  {
    id: 'noise',
    caption: 'Usually noisy — these routinely outnumber the useful types on ordinary prose',
    options: [
      { value: 'time', label: 'Times', singular: 'Time' },
      { value: 'percent', label: 'Percentages', singular: 'Percentage' },
      { value: 'quantity', label: 'Quantities', singular: 'Quantity' },
      { value: 'ordinal', label: 'Ordinals', singular: 'Ordinal' },
      { value: 'cardinal', label: 'Numbers', singular: 'Number' },
    ],
  },
];

/** All 17 canonical types in render order (recommended → standard → noise). */
export const SPACY_CANONICAL_TYPE_OPTIONS: SpacyTypeOption[] =
  SPACY_TYPE_TIERS.flatMap((tier) => tier.options);

export const SPACY_CANONICAL_TYPE_VALUES: string[] =
  SPACY_CANONICAL_TYPE_OPTIONS.map((option) => option.value);

function normalizeTypeToken(label: string): string {
  return label.trim().toLowerCase().replace(/\s+/g, '_');
}

/** The backend's entity-type normalization, using its generated alias table. */
function canonicalEntityType(label: string): string {
  const cleaned = label.trim().toLowerCase();
  const alias = cleaned.replace(/[\s_-]+/g, ' ');
  return Object.hasOwn(ENTITY_TYPE_ALIASES, alias)
    ? ENTITY_TYPE_ALIASES[alias] : cleaned.replace(/\s+/g, '_');
}

/** Hydrate supported aliases into picker values; keep unknowns visible to validation. */
export function normalizeSpacyLabels(labels: readonly string[]): string[] {
  const supported = new Set(SPACY_CANONICAL_TYPE_VALUES);
  return [...new Set(labels.map((label) => {
    const canonical = canonicalEntityType(label);
    return supported.has(canonical) ? canonical : label;
  }))];
}

const SPACY_TYPE_LABELS = new Map(
  SPACY_CANONICAL_TYPE_OPTIONS.map((option) => [option.value, option.label]),
);

/** The readable name for a canonical entity type — the SAME words the NER form
 *  offered when the run was configured, so a user who checked "Facilities" is
 *  not later shown a section headed `FAC`. One table, read from both ends.
 *
 *  Types that are not in the table are humanized rather than dropped or
 *  guessed at: GLiNER and the LLM engines take arbitrary free-text labels, so
 *  "medical_condition" is a perfectly ordinary type to be handed here. A label
 *  that already carries its own capitalization (an acronym, a proper noun) is
 *  left as the user wrote it. */
export function entityTypeLabel(type: string): string {
  const known = SPACY_TYPE_LABELS.get(normalizeTypeToken(type));
  if (known) return known;
  const words = type.trim().replace(/[_-]+/g, ' ').replace(/\s+/g, ' ');
  if (!words) return type.trim();
  return words === words.toLowerCase()
    ? words.charAt(0).toUpperCase() + words.slice(1)
    : words;
}

const SPACY_TYPE_SINGULARS = new Map(
  SPACY_CANONICAL_TYPE_OPTIONS.map((option) => [option.value, option.singular]),
);

/** The readable name for ONE mention of a canonical entity type — the singular
 *  of `entityTypeLabel`.
 *
 *  A chip and a detail header describe a single mention, and the plural form
 *  built for the form's checkboxes reads wrong there ("Ada Lovelace · People").
 *  Unknown types are humanized exactly as `entityTypeLabel` does: GLiNER and
 *  the LLM engines take arbitrary free-text labels, and there is no reliable
 *  singularization of an arbitrary string — so an unregistered type is shown
 *  as the user wrote it rather than mangled by a rule. */
export function entityTypeName(type: string): string {
  return SPACY_TYPE_SINGULARS.get(normalizeTypeToken(type)) ?? entityTypeLabel(type);
}

/** The canonical types currently selected in a comma-joined `labels` value,
 *  in canonical render order.
 *
 *  Backend-supported aliases select their canonical type. Unknown free-text
 *  labels still fail validation rather than silently disappearing. */
export function selectedSpacyTypes(value: string): string[] {
  const wanted = new Set(parseCommaLabels(value).map(canonicalEntityType));
  return SPACY_CANONICAL_TYPE_VALUES.filter((type) => wanted.has(type));
}

/** Canonical values → the comma-joined `labels` wire value, in canonical order. */
export function spacyTypesValue(values: Iterable<string>): string {
  const wanted = new Set(Array.from(values, canonicalEntityType));
  return SPACY_CANONICAL_TYPE_VALUES.filter((type) => wanted.has(type)).join(', ');
}

export const ALL_SPACY_TYPES_VALUE = SPACY_CANONICAL_TYPE_VALUES.join(', ');
export const RECOMMENDED_SPACY_TYPES_VALUE = spacyTypesValue(RECOMMENDED_SPACY_TYPES);

/** Whether `labels` is submittable for the given engine. Required and
 *  non-empty for EVERY engine (C4b): zero-shot engines cannot be asked for
 *  "nothing", and under spaCy an empty filter is not "all types" either — it
 *  used to resolve to a hidden three-type default nobody asked for. */
export function nerLabelsOk(engine: string, labels: string | readonly string[]): boolean {
  const values = typeof labels === 'string' ? parseCommaLabels(labels) : [...labels];
  if (engine !== 'spacy') return values.length > 0;
  const supported = new Set(SPACY_CANONICAL_TYPE_VALUES);
  return values.length > 0 && values.every((type) => supported.has(canonicalEntityType(type)));
}
