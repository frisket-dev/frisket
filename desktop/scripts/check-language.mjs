import { setTimeout as delay } from 'node:timers/promises';

export async function checkLanguageActions(request) {
  const project = await request('/api/projects', {
    method: 'POST', body: JSON.stringify({ name: 'Optional language runtime proof' }),
  });
  const base = `/api/projects/${project.id}`;
  const upload = new FormData();
  upload.append('files', new Blob(['text\n"Barack Obama visited Paris."\n'], { type: 'text/csv' }), 'language.csv');
  upload.append('logical_paths', 'language.csv');
  const plan = await request(`${base}/import/bulk/plan`, { method: 'POST', body: upload });
  const imported = await request(`${base}/import/bulk/${plan.plan_id}/execute`, {
    method: 'POST', body: JSON.stringify({ decisions: {} }),
  });
  if (imported.failed?.length || !imported.first_sheet_id) throw new Error('Language proof import failed.');
  const sheetId = imported.first_sheet_id;
  const data = () => request(`${base}/sheets/${sheetId}/data?offset=0&limit=20`);
  for (const [action_id, params, output_names] of [
    ['map.ner', { source: ['text'], engine: 'spacy', labels: ['person', 'location'] }, { entities: 'mentions' }],
    ['map.translate', { source: ['text'], engine: 'opus_mt', language: ['en'], target_language: 'Spanish' }, { translation: 'spanish' }],
  ]) {
    const launched = await request(`${base}/actions/v1/run`, {
      method: 'POST', body: JSON.stringify({
        action_id, scope: { kind: 'sheet_rows', sheet_id: sheetId }, params, output_names,
        idempotency_key: `native-language-${action_id}`,
      }),
    });
    if (!launched.receipt_id) throw new Error(`${action_id} did not produce a receipt.`);
    const deadline = Date.now() + 5 * 60_000;
    while (true) {
      const receipt = await request(`${base}/actions/v1/receipts/${launched.receipt_id}`);
      if (receipt.status === 'completed') break;
      if (!['queued', 'running'].includes(receipt.status) || Date.now() >= deadline) {
        throw new Error(`${action_id} failed: ${JSON.stringify(receipt.errors)}`);
      }
      await delay(500);
    }
  }
  const sheet = await data();
  const cell = name => {
    const column = sheet.columns.find(column => column.name === name);
    return column && sheet.rows[0]?.cells[String(column.id)];
  };
  const mentions = cell('mentions');
  if (!JSON.stringify(mentions).includes('Barack Obama')) throw new Error('spaCy missed the person fixture.');
  const spanish = String(cell('spanish')).toLowerCase();
  if (!spanish.includes('obama') || !spanish.includes('visit') || spanish === 'barack obama visited paris.') {
    throw new Error('Opus MT did not translate the English fixture into Spanish.');
  }
  console.log('Already-running backend extracted entities and translated using optional language workers.');
}
