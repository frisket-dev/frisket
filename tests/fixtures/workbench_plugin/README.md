# Workbench / Plugin V1 contract fixtures

These fixtures are the executable contract for the V1 workbench/plugin system.

They separate the concepts that the workbench host must keep distinct:

- raw descriptors: static contribution declarations plus runtime-only keys;
- generated manifests: public/static projections with runtime-only keys removed;
- resolved contributions: descriptor + dependency + trust + context + profile
  status with structured diagnostics;
- layout/profile state: persisted project and personal workspace preferences;
- runtime state: selected sheet/row/source/evidence/pane state that is not
  persisted as a profile;
- plugin install state: index entries, install plans, trust prompts, failures,
  and missing-contribution repair;
- migration and route restore: legacy localStorage keys and broken route/layout
  recovery;
- command palette controls: open, reveal, hide, move, and run contribution
  commands.

Canonical region ids are `activityRail`, `leftSidebar`, `rightInspector`,
`bottomDock`, `mainView`, and `modalOrPeek`. Tabs are represented only as host
chrome in placement metadata (`mode: "tab"`), including main-view table tabs,
detail tabs, and bottom dock tabs. No fixture introduces bespoke `BottomTab`,
`MainViewTab`, or `DetailTab` contribution kinds.

## Files

- `descriptors.json` covers first-party and plugin-provided descriptors,
  manifest projection, legal placements, route bindings, and runtime-only
  fields that must be stripped from generated manifests.
- `layout_profile_runtime.json` covers default/project/personal profile merge,
  hidden-not-uninstalled state, split mainView roundtrip, runtime cross-pane
  state, legacy localStorage migration, route restore failure, malformed
  persisted layout repair, alias layout rewrites, and main-view table tabs.
- `plugin_install_placeholders.json` covers plugin discovery/install/trust,
  trusted-local activation, install failure, missing placeholders, repair
  actions, trust-blocked installed plugins, incompatible versions, and runtime
  activation failure.
- `command_activity_bottom_detail.json` covers command palette contribution
  control, activity rail recovery, bottomDock tab chrome, row/entity/source
  detail tabs, modal/peek evidence placement, and disabled map view context.
- `expected_matrix.json` is a compact index of all required cases and the
  expected validator outcomes.
