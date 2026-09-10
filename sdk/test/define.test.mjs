import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  capability,
  defineCommand,
  definePanel,
  definePlugin,
  defineView,
  needs,
} from '../dist/index.mjs';

const basePanel = () =>
  definePanel({
    key: 'inspector',
    title: 'Demo Inspector',
    component: 'InspectorPanel',
    placements: [{ host: 'rightInspector', mode: 'panel', order: 40 }],
    requires: [capability('sheet.active')],
    needs: [needs.activeSheet()],
  });

test('definePlugin emits exactly the loader artifact shapes', () => {
  const { manifest, descriptors } = definePlugin({
    id: 'demo.sdk_shape',
    version: '0.1.0',
    panels: [basePanel()],
    views: [
      defineView({
        key: 'main',
        title: 'Demo View',
        component: 'MainView',
        placements: [{ host: 'mainView', mode: 'pane', default: true, order: 20 }],
        requires: [capability('sheet.rows.read')],
        needs: [needs.activeSheet()],
      }),
    ],
    commands: [
      defineCommand({ key: 'hello', title: 'Say Hello', component: 'HelloCommand' }),
    ],
  });

  assert.equal(manifest.schema_version, 'frisket.plugin.v1');
  assert.deepEqual(manifest.contributes.workbench_panels, ['demo.sdk_shape.panel.inspector']);
  assert.deepEqual(manifest.contributes.workbench_views, ['demo.sdk_shape.view.main']);
  assert.deepEqual(manifest.contributes.workbench_commands, ['demo.sdk_shape.command.hello']);
  assert.deepEqual(manifest.contributes.actions, []);
  assert.deepEqual(manifest.runtime.actions, []);
  assert.equal(manifest.runtime.workbench_components.length, 3);
  assert.equal(descriptors.schemaVersion, 'frisket.workbench_descriptor_package.v1');
  const command = descriptors.descriptors.find((d) => d.kind === 'command');
  assert.equal(command.commandId, 'demo.sdk_shape.command.hello');
  assert.equal(command.placements[0].slot, 'launcher');
  const panel = descriptors.descriptors.find((d) => d.kind === 'panel');
  assert.equal(panel.placements[0].slot, 'inspection');
  assert.deepEqual(panel.dataRequirements, [{ kind: 'activeSheet' }]);
});

test('kind-illegal placements throw with the legal table in the message', () => {
  assert.throws(
    () =>
      definePlugin({
        id: 'demo.illegal',
        version: '0.1.0',
        views: [
          defineView({
            key: 'bad',
            title: 'Bad',
            component: 'Bad',
            placements: [{ host: 'rightInspector', mode: 'panel' }],
          }),
        ],
      }),
    /not legal for plugin views.*mainView:pane/,
  );
});

test('function-valued fields are rejected by construction', () => {
  const panel = basePanel();
  panel.needs.push({ kind: 'activeSheet', appearsWhen: () => true });
  assert.throws(
    () => definePlugin({ id: 'demo.callbacks', version: '0.1.0', panels: [panel] }),
    /is a function.*declarative/,
  );
});

test('reserved plugin ids are rejected', () => {
  for (const id of ['frisket', 'frisket.core']) {
    assert.throws(
      () => definePlugin({ id, version: '0.1.0', panels: [basePanel()] }),
      /reserved/,
    );
  }
});

test('duplicate contribution ids are rejected', () => {
  assert.throws(
    () =>
      definePlugin({
        id: 'demo.dupes',
        version: '0.1.0',
        panels: [basePanel(), basePanel()],
      }),
    /duplicate contribution id/,
  );
});

test('needs helpers compile to declarative data', () => {
  assert.deepEqual(needs.selectedRows(2), { kind: 'selectedRows', min: 2 });
  assert.deepEqual(needs.sheetHasColumnType('geo_point'), {
    kind: 'sheetHasColumnType',
    columnType: 'geo_point',
  });
  assert.deepEqual(capability('grid.state.read', { optional: true }), {
    kind: 'hostCapability',
    id: 'grid.state.read',
    optional: true,
  });
});
