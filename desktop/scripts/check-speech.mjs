import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

// Exercise the backend that was already running when engine setup began.
export async function checkSpeechActions(request, speech) {
  const project = await request('/api/projects', {
    method: 'POST', body: JSON.stringify({ name: 'Optional speech runtime proof' }),
  });
  const base = `/api/projects/${project.id}`;
  const upload = new FormData();
  upload.append('files', new Blob([await readFile(speech)], { type: 'audio/wav' }), path.basename(speech));
  upload.append('logical_paths', path.basename(speech));
  const plan = await request(`${base}/import/bulk/plan`, { method: 'POST', body: upload });
  const imported = await request(`${base}/import/bulk/${plan.plan_id}/execute`, {
    method: 'POST', body: JSON.stringify({ decisions: {} }),
  });
  if (imported.failed?.length || !imported.first_sheet_id) throw new Error('Speech proof import failed.');
  const sheetId = imported.first_sheet_id;
  const data = () => request(`${base}/sheets/${sheetId}/data?offset=0&limit=20`);
  const deadline = Date.now() + 15 * 60_000;
  let sheet;
  do {
    sheet = await data();
    if (sheet.rows.length) break;
    if (Date.now() >= deadline) throw new Error('Speech proof import timed out.');
    await delay(500);
  } while (true);
  const source = sheet.columns.find(column => ['audio', 'file', 'video'].includes(column.type));
  if (!source) throw new Error('Speech proof import did not produce a media column.');
  for (const engine of ['parakeet-tdt', 'faster_whisper']) {
    const output = engine === 'parakeet-tdt' ? 'parakeet_text' : 'whisper_text';
    const launched = await request(`${base}/actions/v1/run`, {
      method: 'POST', body: JSON.stringify({
        action_id: 'media.transcribe', scope: { kind: 'sheet_rows', sheet_id: sheetId },
        params: { source: source.name, engine, ...(engine === 'faster_whisper' ? { model_size: 'base', language: 'en' } : {}) },
        output_names: { text: output, segments: `${output}_segments` },
        idempotency_key: `native-speech-${engine}`,
      }),
    });
    if (!launched.receipt_id) throw new Error(`${engine} did not queue a receipt.`);
    let receipt;
    do {
      receipt = await request(`${base}/actions/v1/receipts/${launched.receipt_id}`);
      if (receipt.status === 'completed') break;
      if (!['queued', 'running'].includes(receipt.status)) throw new Error(`${engine} failed: ${JSON.stringify(receipt.errors)}`);
      if (Date.now() >= deadline) throw new Error(`${engine} transcription timed out.`);
      await delay(1000);
    } while (true);
    sheet = await data();
    const column = sheet.columns.find(column => column.name === output);
    const text = column && sheet.rows[0].cells[String(column.id)];
    if (!String(text).toLowerCase().replace(/[^a-z0-9]/g, '').includes('lookingforward')) {
      throw new Error(`${engine} missed the checked-in speech fixture sentinel.`);
    }
  }
  console.log('Already-running backend transcribed with both optional speech engines.');
}
