import { describe, expect, it } from 'vitest';

import { actionTemplatesFromCatalog } from '../../src/actions/model';
import type { ActionCatalogEntry, ActionCatalogPayload } from '../../src/api/types';
import * as presentationOwner from '../../src/components/action-panel/actionPresentation';
import { completeMappedActionCatalog } from '../support/actionCatalogFixtures';
import {
  hasServedActionCatalogPython,
  servedActionCatalog,
} from '../support/servedActionCatalog';

const hasServedCatalog = hasServedActionCatalogPython();
const catalog: ActionCatalogPayload = hasServedCatalog || process.env.CI
  ? servedActionCatalog()
  : completeMappedActionCatalog();
const describeServedCatalog = hasServedCatalog || process.env.CI
  ? describe
  : describe.skip;
const OWNER_PATH = '../../src/actions/savedActionSpec.ts';

interface DecodedSavedActionSpec {
  entry: ActionCatalogEntry;
  params: Record<string, unknown>;
}

interface SavedActionSpecOwner {
  decodeSavedActionSpec(
    payload: ActionCatalogPayload,
    envelope: unknown,
  ): DecodedSavedActionSpec;
  encodeSavedActionSpec(decoded: DecodedSavedActionSpec): Record<string, unknown>;
  SAVED_ACTION_SPEC_REFUSAL: string;
  SavedActionSpecError: new (...args: never[]) => Error;
}

// Eager globbing executes the real owner when it exists but still lets this
// RED suite collect when it does not. Absence and missing exports therefore
// fail as named assertions, never as a module-resolution collection error.
const ownerModules = import.meta.glob('../../src/actions/savedActionSpec.ts', {
  eager: true,
});

function owner(): SavedActionSpecOwner {
  const candidate = ownerModules[OWNER_PATH] as Partial<SavedActionSpecOwner> | undefined;
  if (!candidate) throw new Error(`Missing saved-spec owner ${OWNER_PATH}`);
  return candidate as SavedActionSpecOwner;
}

type CanonicalSchemaDraft = Record<string, unknown>;
type BuildCanonicalDraftFromValidatedParams = (
  entry: ActionCatalogEntry,
  params: Readonly<Record<string, unknown>>,
) => CanonicalSchemaDraft;

function schemaDraftBuilder(): BuildCanonicalDraftFromValidatedParams {
  const candidate = (
    presentationOwner as typeof presentationOwner & {
      buildCanonicalDraftFromValidatedParams?: BuildCanonicalDraftFromValidatedParams;
    }
  ).buildCanonicalDraftFromValidatedParams;
  if (typeof candidate !== 'function') {
    throw new Error(
      'actionPresentation must own buildCanonicalDraftFromValidatedParams',
    );
  }
  return candidate;
}

const serializeSchemaDraft = presentationOwner.serializeCanonicalDraft as unknown as (
  draft: CanonicalSchemaDraft,
) => Record<string, unknown>;

// Untouched legacy definitions without examples use test-owned,
// minimal canonical params, validated against the same input_schema by the
// generated matrix below. A new no-example kind deliberately fails the
// matrix until it supplies an example or an equally explicit seed.
const NO_EXAMPLE_PARAMS: Readonly<Record<string, Record<string, unknown>>> = {};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function canonicalParams(entry: ActionCatalogEntry): Record<string, unknown> {
  const example = entry.examples[0];
  const params = isRecord(example) && isRecord(example.params)
    ? example.params
    : NO_EXAMPLE_PARAMS[entry.kind];
  if (!params) {
    throw new Error(`No canonical saved-spec params seed for ${entry.kind}`);
  }
  return structuredClone(params);
}

function envelopeFor(entry: ActionCatalogEntry) {
  const createsSheet = entry.ui_hints.typed_action?.creates_sheet === true;
  const params = canonicalParams(entry);
  const fields = Array.isArray(params.fields) ? params.fields : [];
  const outputs = fields.length > 0 ? fields.map((field) => String(field.name))
    : (entry.ui_hints.logical_outputs ?? []).map(({ key }) => key);
  return {
    action_id: entry.kind,
    scope: createsSheet ? { kind: 'project' } : { kind: 'sheet_rows', sheet_id: 7 },
    params,
    output_names: Object.fromEntries(outputs.map((key) => [key, key])),
    ...(createsSheet ? { sheet_name: 'Saved destination' } : {}),
  };
}

function expectRefusal(payload: ActionCatalogPayload, envelope: unknown): void {
  const codec = owner();
  let thrown: unknown;
  try {
    codec.decodeSavedActionSpec(payload, envelope);
  } catch (error) {
    thrown = error;
  }
  expect(thrown).toBeInstanceOf(codec.SavedActionSpecError);
  expect((thrown as Error).message).toBe(codec.SAVED_ACTION_SPEC_REFUSAL);
}

describeServedCatalog('saved ActionSpec v1 reset', () => {
  it.each(['derive.join', 'derive.link_table', 'map.api_call', 'map.find_topic_sections', 'import.urls',
    'media.extract_pdf_tables', 'derive.transcript_segments', 'media.fetch_url',
    'web.capture_screenshot', 'media.video_frames', 'media.extract_faces', 'media.to_markdown'])(
    'round-trips the served typed %s example with reusable names and scope', (kind) => {
      const entry = catalog.actions.find((candidate) => candidate.kind === kind);
      if (!entry) throw new Error(`Missing ${kind}`);
      expect(entry.ui_hints.form).toBe('generated');
      const createsSheet = entry.ui_hints.typed_action?.creates_sheet === true;
      const projectScoped = createsSheet && entry.row_scope_policy?.kind !== 'sheet_rows';
      const draft = {
        action_id: kind,
        scope: projectScoped ? { kind: 'project' } : { kind: 'sheet_rows', sheet_id: 7, row_ids: [3, 8] },
        params: canonicalParams(entry),
        output_names: kind === 'import.urls' ? { media: 'Downloaded files' }
          : Object.fromEntries((entry.ui_hints.logical_outputs ?? []).map(({ key }) => [key, `Saved ${key}`])),
        ...(createsSheet ? { sheet_name: 'Saved destination' } : {}),
      };
      expect(owner().encodeSavedActionSpec(owner().decodeSavedActionSpec(catalog, draft))).toEqual(draft);
      expectRefusal(catalog, { ...draft, confirmation: 'old-consent' });
      expectRefusal(catalog, { ...draft, idempotency_key: 'old-invocation' });
      expectRefusal(catalog, { ...draft, unexpected: 'stale-metadata' });
      expectRefusal(catalog, { ...draft, replace_existing: true });
    },
  );
  it('collects and names the one executable codec owner', () => {
    expect(Object.keys(ownerModules)).toEqual([OWNER_PATH]);
    const codec = owner();
    expect(codec.decodeSavedActionSpec).toBeTypeOf('function');
    expect(codec.encodeSavedActionSpec).toBeTypeOf('function');
    expect(codec.SavedActionSpecError).toBeTypeOf('function');
    expect(codec.SAVED_ACTION_SPEC_REFUSAL).toBe(
      'This saved action uses an unsupported version or invalid parameters. Recreate the action.',
    );
  });

  it('owns one generic catalog-schema param-state initializer', () => {
    expect(
      (presentationOwner as Record<string, unknown>)
        .buildCanonicalDraftFromValidatedParams,
    ).toBeTypeOf('function');
  });

  it('round-trips typed transforms through decoded params, canonical form state, and serialization', () => {
    const codec = owner();
    const buildSchemaDraft = schemaDraftBuilder();
    expect(new Set(catalog.actions.map((entry) => entry.kind)).size)
      .toBe(catalog.actions.length);

    for (const entry of catalog.actions.filter((candidate) => (
      ['map.clean_column', 'map.template'].includes(candidate.kind)
    ))) {
      const envelope = envelopeFor(entry);
      const before = structuredClone(envelope);
      const decoded = codec.decodeSavedActionSpec(catalog, envelope);
      const formState = buildSchemaDraft(decoded.entry, decoded.params);

      expect(decoded.entry, entry.kind).toBe(entry);
      expect(decoded.params, entry.kind).toEqual(envelope.params);
      expect(formState, `${entry.kind} reused the decoded params object`).not.toBe(decoded.params);
      expect(serializeSchemaDraft(formState), entry.kind).toEqual(envelope.params);
      expect(envelope, `${entry.kind} was mutated while validating`).toEqual(before);
      expect(codec.encodeSavedActionSpec(decoded), entry.kind).toEqual(envelope);
    }
  });

  it('partitions every drawer kind through its real presentation hydration hook', () => {
    const templates = actionTemplatesFromCatalog(catalog);
    const dispositions = presentationOwner.deriveActionPresentationCatalog(
      catalog,
      templates,
    );
    let drawerAddressable = 0;
    let hydrated = 0;
    let generic = 0;
    let dedicated = 0;
    let hidden = 0;
    let generated = 0;
    const generatedKinds = new Set<string>();

    for (const entry of catalog.actions) {
      const disposition = dispositions.get(entry.kind);
      expect(disposition, entry.kind).toBeDefined();
      if (disposition?.kind === 'hidden') {
        hidden += 1;
        continue;
      }
      if (entry.ui_hints.form === 'generated') {
        expect(disposition?.kind, entry.kind).toBe('generated');
        generated += 1;
        generatedKinds.add(entry.kind);
        continue;
      }
      drawerAddressable += 1;
      if (disposition?.kind === 'dedicated') {
        dedicated += 1;
        continue;
      }
      expect(disposition?.kind, entry.kind).toBe('generic');
      generic += 1;
      const template = templates.find((candidate) => candidate.actionKind === entry.kind);
      expect(template, entry.kind).toBeDefined();
      if (!template) continue;
      const params = canonicalParams(entry);
      const declaredNames = new Set((template.params ?? []).map((param) => param.name));
      const scalarEdits = Object.fromEntries(Object.entries(params).filter(([name, value]) => (
        declaredNames.has(name)
        && (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean')
      )));
      const hydratedDraft = presentationOwner.buildCanonicalDraft(template, scalarEdits);

      expect(presentationOwner.serializeCanonicalDraft(hydratedDraft), entry.kind)
        .toEqual(scalarEdits);
      hydrated += 1;
    }

    expect(drawerAddressable + hidden + generated).toBe(catalog.actions.length);
    expect(hydrated).toBe(generic);
    expect(drawerAddressable).toBe(hydrated + dedicated);
    // Retired legacy fields are not a coverage floor: these callers now use
    // the generated host and the exact saved-request roundtrips above.
    for (const kind of ['media.fetch_url', 'web.capture_screenshot', 'media.video_frames', 'media.extract_faces']) {
      expect(generatedKinds.has(kind), kind).toBe(true);
    }
  });

  it.each([
    [
      'pre-floor version',
      () => {
        const entry = catalog.actions.find((candidate) => candidate.kind === 'map.clean_column');
        if (!entry) throw new Error('Missing map.clean_column fixture entry');
        return { ...envelopeFor(entry), authoring_contract_version: 0 };
      },
    ],
    [
      'schema-invalid coercible params',
      () => {
        const entry = catalog.actions.find((candidate) => candidate.kind === 'map.clean_column');
        if (!entry) throw new Error('Missing map.clean_column fixture entry');
        return {
          ...envelopeFor(entry),
          params: { ...canonicalParams(entry), sheet_id: '1' },
        };
      },
    ],
    [
      'schema-invalid additional params',
      () => {
        const entry = catalog.actions.find((candidate) => candidate.kind === 'map.clean_column');
        if (!entry) throw new Error('Missing map.clean_column fixture entry');
        return {
          ...envelopeFor(entry),
          params: { ...canonicalParams(entry), legacy_alias: true },
        };
      },
    ],
    [
      'non-integer version',
      () => {
        const entry = catalog.actions.find((candidate) => candidate.kind === 'map.clean_column');
        if (!entry) throw new Error('Missing map.clean_column fixture entry');
        return { ...envelopeFor(entry), authoring_contract_version: '1' };
      },
    ],
    [
      'fractional version',
      () => {
        const entry = catalog.actions.find((candidate) => candidate.kind === 'map.clean_column');
        if (!entry) throw new Error('Missing map.clean_column fixture entry');
        return { ...envelopeFor(entry), authoring_contract_version: 1.5 };
      },
    ],
  ] as const)('refuses %s with the one exact recreate message', (_label, makeEnvelope) => {
    expectRefusal(catalog, makeEnvelope());
    expect(owner().SAVED_ACTION_SPEC_REFUSAL).toBe(
      'This saved action uses an unsupported version or invalid parameters. Recreate the action.',
    );
  });

  it.each(['action_id', 'scope', 'params'] as const)(
    'refuses an envelope missing top-level %s',
    (missingKey) => {
      const entry = catalog.actions[0];
      const envelope: Record<string, unknown> = envelopeFor(entry);
      delete envelope[missingKey];
      expectRefusal(catalog, envelope);
    },
  );

  it.each([
    [
      'an unknown catalog kind',
      () => {
        const entry = catalog.actions[0];
        return { payload: catalog, envelope: { ...envelopeFor(entry), action_id: 'map.unknown' } };
      },
    ],
    [
      'a kind with duplicate catalog matches',
      () => {
        const entry = catalog.actions[0];
        return {
          payload: { ...catalog, actions: [...catalog.actions, entry] },
          envelope: envelopeFor(entry),
        };
      },
    ],
    [
      'an envelope with an extra top-level key',
      () => {
        const entry = catalog.actions[0];
        return { payload: catalog, envelope: { ...envelopeFor(entry), legacy_alias: true } };
      },
    ],
  ] as const)('refuses %s rather than guessing or repairing', (_label, makeAttempt) => {
    const { payload, envelope } = makeAttempt();
    expectRefusal(payload, envelope);
  });

  it('rejects legacy saved envelopes instead of inferring scope or output names', () => {
    const entry = catalog.actions.find((candidate) => candidate.kind === 'map.clean_column')!;
    for (const version of [1, 2]) {
      expectRefusal(catalog, {
        action_kind: entry.kind,
        authoring_contract_version: version,
        params: canonicalParams(entry),
      });
    }
  });

  it('round-trips registered drafts without destructive replacement consent', () => {
    const codec = owner();
    const entry = catalog.actions.find((candidate) => (
      candidate.kind === 'map.template' && candidate.ui_hints.form === 'generated'
    ));
    if (!entry) throw new Error('Missing generated map.template fixture entry');
    const draft = {
      action_id: entry.kind,
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: canonicalParams(entry),
      output_names: Object.fromEntries(
        (entry.ui_hints.logical_outputs ?? []).map(({ key }) => [key, key]),
      ),
    };

    expect(codec.encodeSavedActionSpec(codec.decodeSavedActionSpec(catalog, draft))).toEqual(draft);
    expectRefusal(catalog, { ...draft, replace_existing: true });
  });
});
