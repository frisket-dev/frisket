// src/contracts.gen.ts
var PLUGIN_LEGAL_PLACEMENTS = {
  "command": [
    {
      "host": "commandPalette",
      "mode": "command"
    }
  ],
  "panel": [
    {
      "host": "rightInspector",
      "mode": "panel"
    },
    {
      "host": "bottomDock",
      "mode": "tab"
    },
    {
      "host": "leftSidebar",
      "mode": "panel"
    },
    {
      "host": "rowDetail",
      "mode": "tab"
    },
    {
      "host": "columnDetail",
      "mode": "tab"
    },
    {
      "host": "columnInspector",
      "mode": "section"
    },
    {
      "host": "entityDetail",
      "mode": "tab"
    },
    {
      "host": "sourceDetail",
      "mode": "tab"
    },
    {
      "host": "modalOrPeek",
      "mode": "peek"
    },
    {
      "host": "activityRail",
      "mode": "command"
    }
  ],
  "view": [
    {
      "host": "mainView",
      "mode": "pane"
    },
    {
      "host": "activityRail",
      "mode": "command"
    }
  ]
};
var WORKBENCH_SLOT_BY_HOST = {
  "activityRail": "launcher",
  "bottomDock": "companion.output",
  "columnDetail": "detail",
  "columnInspector": "detail",
  "commandPalette": "launcher",
  "entityDetail": "detail",
  "leftSidebar": "scope",
  "mainView": "work.primary",
  "modalOrPeek": "interruption",
  "rightInspector": "inspection",
  "rowDetail": "detail",
  "rowInspector": "detail",
  "sourceDetail": "detail"
};
var DATA_REQUIREMENT_KINDS = [
  "activeProject",
  "activeSheet",
  "activeRow",
  "activeColumn",
  "activeCell",
  "activeEvidence",
  "activeSource",
  "activeEntity",
  "activeProjection",
  "selectedRows",
  "sheetHasColumnType"
];
var CAPABILITY_DEFAULTS = {
  "panel": [
    "sheet.active",
    "selection.rows",
    "host.navigation.openRow",
    "grid.state.read",
    "action.run"
  ],
  "projectionView": [
    "projection.status",
    "projection.build",
    "projection.artifact.read",
    "projection.data.read",
    "host.navigation.openRow",
    "grid.state.read",
    "grid.filter.applyBbox",
    "action.run"
  ],
  "view": [
    "sheet.rows.read",
    "media.blob.resolve",
    "host.navigation.openRow",
    "grid.state.read",
    "grid.filter.applyBbox",
    "action.run"
  ]
};
var CONTEXT_SCHEMA_VERSIONS = [
  "frisket.plugin_view_context.v1",
  "frisket.plugin_panel_context.v1",
  "frisket.plugin_dock_tab_context.v1",
  "frisket.plugin_detail_context.v1",
  "frisket.plugin_peek_context.v1",
  "frisket.plugin_command_context.v1",
  "frisket.plugin_projection_view_context.v1"
];
var DESCRIPTOR_SCHEMA_VERSIONS = {
  "command": "frisket.command.v1",
  "descriptorPackage": "frisket.workbench_descriptor_package.v1",
  "panel": "frisket.workbench.panel.v1",
  "plugin": "frisket.plugin.v1",
  "view": "frisket.workbench.view.v1"
};
var RESERVED_PLUGIN_IDS = [
  "frisket",
  "frisket.core"
];

// src/contracts.ts
var PLUGIN_LEGAL_PLACEMENTS2 = {
  panel: [...PLUGIN_LEGAL_PLACEMENTS.panel],
  view: [...PLUGIN_LEGAL_PLACEMENTS.view],
  command: [...PLUGIN_LEGAL_PLACEMENTS.command]
};
var WORKBENCH_SLOT_BY_HOST2 = {
  ...WORKBENCH_SLOT_BY_HOST
};
var DATA_REQUIREMENT_KINDS2 = [
  ...DATA_REQUIREMENT_KINDS
];
var CAPABILITY_DEFAULTS2 = CAPABILITY_DEFAULTS;
var CONTEXT_SCHEMA_VERSIONS2 = CONTEXT_SCHEMA_VERSIONS;
var DESCRIPTOR_SCHEMA_VERSIONS2 = DESCRIPTOR_SCHEMA_VERSIONS;
var RESERVED_PLUGIN_IDS2 = RESERVED_PLUGIN_IDS;
var PLUGIN_ID_PATTERN = /^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?)*$/;

// src/define.ts
var needs = {
  activeProject: () => ({ kind: "activeProject" }),
  activeSheet: () => ({ kind: "activeSheet" }),
  activeRow: () => ({ kind: "activeRow" }),
  activeColumn: () => ({ kind: "activeColumn" }),
  activeCell: () => ({ kind: "activeCell" }),
  activeEvidence: () => ({ kind: "activeEvidence" }),
  activeSource: () => ({ kind: "activeSource" }),
  activeEntity: () => ({ kind: "activeEntity" }),
  activeProjection: () => ({ kind: "activeProjection" }),
  selectedRows: (min) => min === void 0 ? { kind: "selectedRows" } : { kind: "selectedRows", min },
  sheetHasColumnType: (columnType) => ({
    kind: "sheetHasColumnType",
    columnType
  })
};
function capability(id, options) {
  if (typeof id !== "string" || id.length === 0) {
    throw new Error("capability(id): id must be a non-empty string");
  }
  return options?.optional ? { kind: "hostCapability", id, optional: true } : { kind: "hostCapability", id };
}
var KEY_PATTERN = /^[a-z0-9][a-z0-9_]*$/;
function assertKey(kind, key) {
  if (!KEY_PATTERN.test(key)) {
    throw new Error(
      `${kind} key "${key}" is invalid: keys are lowercase alphanumeric/underscore (the full id becomes <pluginId>.<kind>.<key>)`
    );
  }
}
function assertNoFunctions(value, path) {
  if (typeof value === "function") {
    throw new Error(
      `${path} is a function \u2014 descriptors are declarative data; use needs.* / capability() helpers instead of callbacks`
    );
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertNoFunctions(item, `${path}[${index}]`));
  } else if (value !== null && typeof value === "object") {
    for (const [k, v] of Object.entries(value)) assertNoFunctions(v, `${path}.${k}`);
  }
}
function assertLegalPlacements(kind, id, placements) {
  if (placements.length === 0) {
    throw new Error(`${kind} "${id}" declares no placements`);
  }
  const legal = PLUGIN_LEGAL_PLACEMENTS2[kind];
  for (const placement of placements) {
    const ok = legal.some(
      (pair) => pair.host === placement.host && pair.mode === placement.mode
    );
    if (!ok) {
      const legalText = legal.map((pair) => `${pair.host}:${pair.mode}`).join(", ");
      throw new Error(
        `${kind} "${id}" placement ${placement.host}:${placement.mode} is not legal for plugin ${kind}s (legal: ${legalText})`
      );
    }
  }
}
function definePanel(definition) {
  assertKey("panel", definition.key);
  return { kind: "panel", ...definition };
}
function defineView(definition) {
  assertKey("view", definition.key);
  return { kind: "view", ...definition };
}
function defineCommand(definition) {
  assertKey("command", definition.key);
  return { kind: "command", ...definition };
}
function defineProjection(definition) {
  assertKey("projection", definition.key);
  return { kind: "projection", ...definition };
}
function normalizePlacement(placement) {
  const slot = placement.slot ?? WORKBENCH_SLOT_BY_HOST2[placement.host];
  const out = {
    host: placement.host,
    mode: placement.mode,
    slot
  };
  if (placement.placementId !== void 0) out.placementId = placement.placementId;
  if (placement.order !== void 0) out.order = placement.order;
  if (placement.default !== void 0) out.default = placement.default;
  return out;
}
function definePlugin(definition) {
  const { id: pluginId, version } = definition;
  if (!PLUGIN_ID_PATTERN.test(pluginId)) {
    throw new Error(`plugin id "${pluginId}" does not match the plugin id grammar`);
  }
  if (RESERVED_PLUGIN_IDS2.includes(pluginId)) {
    throw new Error(
      `plugin id "${pluginId}" is reserved (first-party contribution namespace); pick your own namespace`
    );
  }
  const modulePath = definition.module ?? "frontend/plugin.js";
  const moduleKey = `${pluginId}.ui`;
  const views = definition.views ?? [];
  const panels = definition.panels ?? [];
  const commands = definition.commands ?? [];
  const projections = definition.projections ?? [];
  const descriptors = [];
  const components = [];
  const seenIds = /* @__PURE__ */ new Set();
  const claimId = (contributionId) => {
    if (seenIds.has(contributionId)) {
      throw new Error(`duplicate contribution id "${contributionId}"`);
    }
    seenIds.add(contributionId);
    return contributionId;
  };
  const pushComponent = (contributionId, componentKind, exportName) => {
    components.push({
      contribution_id: contributionId,
      module_key: moduleKey,
      component_key: `${pluginId}.${componentKind}.${exportName}`,
      module_path: modulePath
    });
  };
  for (const view of views) {
    const contributionId = claimId(`${pluginId}.view.${view.key}`);
    assertLegalPlacements("view", contributionId, view.placements);
    if (view.projectionKind && !(view.requires ?? []).some(
      (requirement) => requirement.kind === "hostCapability" && requirement.id === "projection.status"
    )) {
      throw new Error(
        `view "${contributionId}" declares projectionKind but not capability('projection.status') \u2014 the host drops projection views without it`
      );
    }
    const descriptor = {
      schemaVersion: DESCRIPTOR_SCHEMA_VERSIONS2.view,
      id: contributionId,
      kind: "view",
      title: view.title,
      ...view.shortTitle !== void 0 ? { shortTitle: view.shortTitle } : {},
      ...view.icon !== void 0 ? { icon: view.icon } : {},
      ownerPluginId: pluginId,
      componentKey: `${pluginId}.components.${view.component}`,
      placements: view.placements.map(normalizePlacement),
      requires: view.requires ?? [],
      dataRequirements: view.needs ?? [],
      ...view.projectionKind !== void 0 ? { projectionKind: view.projectionKind } : {},
      ...view.projectionParams !== void 0 ? { projectionParams: view.projectionParams } : {}
    };
    descriptors.push(descriptor);
    pushComponent(contributionId, "components", view.component);
  }
  for (const panel of panels) {
    const contributionId = claimId(`${pluginId}.panel.${panel.key}`);
    assertLegalPlacements("panel", contributionId, panel.placements);
    descriptors.push({
      schemaVersion: DESCRIPTOR_SCHEMA_VERSIONS2.panel,
      id: contributionId,
      kind: "panel",
      title: panel.title,
      ...panel.shortTitle !== void 0 ? { shortTitle: panel.shortTitle } : {},
      ...panel.icon !== void 0 ? { icon: panel.icon } : {},
      ownerPluginId: pluginId,
      componentKey: `${pluginId}.components.${panel.component}`,
      placements: panel.placements.map(normalizePlacement),
      requires: panel.requires ?? [],
      dataRequirements: panel.needs ?? []
    });
    pushComponent(contributionId, "components", panel.component);
  }
  for (const command of commands) {
    const contributionId = claimId(`${pluginId}.command.${command.key}`);
    const placements = command.placements ?? [
      { host: "commandPalette", mode: "command" }
    ];
    assertLegalPlacements("command", contributionId, placements);
    descriptors.push({
      schemaVersion: DESCRIPTOR_SCHEMA_VERSIONS2.command,
      id: contributionId,
      kind: "command",
      title: command.title,
      ...command.shortTitle !== void 0 ? { shortTitle: command.shortTitle } : {},
      ...command.icon !== void 0 ? { icon: command.icon } : {},
      ownerPluginId: pluginId,
      componentKey: `${pluginId}.commands.${command.component}`,
      commandId: contributionId,
      handlerKey: `${pluginId}.commands.${command.component}`,
      placements: placements.map(normalizePlacement),
      requires: command.requires ?? [],
      dataRequirements: command.needs ?? []
    });
    pushComponent(contributionId, "commands", command.component);
  }
  const runtimeProjections = projections.map((projection) => {
    const projectionKind = claimId(`${pluginId}.projection.${projection.key}`);
    return {
      kind: projectionKind,
      handler_key: `${pluginId}:${projection.handler}`,
      handler_api: "plugin_projection",
      title: projection.title,
      module_path: projection.modulePath ?? "plugin.py",
      // Planning only; projection data stays host-owned.
      execution: {
        mode: "runtime_plan",
        ...projection.role !== void 0 ? { role: projection.role } : {}
      }
    };
  });
  const manifest = {
    schema_version: DESCRIPTOR_SCHEMA_VERSIONS2.plugin,
    id: pluginId,
    version,
    contributes: {
      workbench_views: views.map((view) => `${pluginId}.view.${view.key}`),
      workbench_panels: panels.map((panel) => `${pluginId}.panel.${panel.key}`),
      workbench_commands: commands.map((command) => `${pluginId}.command.${command.key}`),
      // Actions are declared natively in Python (`Plugin(actions=(...))`), which
      // generates their manifest entries; this SDK authors workbench and
      // projection contributions only, so it always emits the key empty.
      actions: [],
      importers: [],
      operators: [],
      projections: runtimeProjections.map((projection) => projection.kind),
      column_types: [],
      job_handlers: []
    },
    requires: {
      capabilities: definition.capabilities ?? [],
      secrets: definition.secrets ?? []
    },
    runtime: {
      actions: [],
      importers: [],
      operators: [],
      projections: runtimeProjections,
      job_handlers: [],
      workbench_components: components
    }
  };
  const descriptorPackage = {
    schemaVersion: DESCRIPTOR_SCHEMA_VERSIONS2.descriptorPackage,
    descriptors
  };
  assertNoFunctions(manifest, "manifest");
  assertNoFunctions(descriptorPackage, "descriptors");
  return { manifest, descriptors: descriptorPackage };
}
export {
  CAPABILITY_DEFAULTS2 as CAPABILITY_DEFAULTS,
  CONTEXT_SCHEMA_VERSIONS2 as CONTEXT_SCHEMA_VERSIONS,
  DATA_REQUIREMENT_KINDS2 as DATA_REQUIREMENT_KINDS,
  DESCRIPTOR_SCHEMA_VERSIONS2 as DESCRIPTOR_SCHEMA_VERSIONS,
  PLUGIN_ID_PATTERN,
  PLUGIN_LEGAL_PLACEMENTS2 as PLUGIN_LEGAL_PLACEMENTS,
  RESERVED_PLUGIN_IDS2 as RESERVED_PLUGIN_IDS,
  WORKBENCH_SLOT_BY_HOST2 as WORKBENCH_SLOT_BY_HOST,
  capability,
  defineCommand,
  definePanel,
  definePlugin,
  defineProjection,
  defineView,
  needs
};
