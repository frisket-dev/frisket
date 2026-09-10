import { afterEach, describe, expect, it, vi } from 'vitest';

import { createPreviewComparisonsApi } from '../../src/api/previewComparisons';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('scratch comparison generated HTTP contracts', () => {
  it('sends each scratch producer through native FormData with file then JSON payload', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      if (String(input).endsWith('/estimate')) return jsonResponse({
        schema_version: 'frisket.action_estimate_result.v1',
        estimate: { rows: 1, cost: 0.01, cost_source: 'provider', billed_cost: 10_000, policy_id: 'test' },
      });
      return jsonResponse({
        schema_version: 'frisket.action_preview.v1', preview_id: `scratch-${requests.length}`, total: 1,
      }, 202);
    }));
    const api = createPreviewComparisonsApi(
      (status) => new Error(String(status)),
      'project/one',
    );
    const file = new File(['sample'], 'sample.pdf', { type: 'application/pdf' });

    await api.estimateOcrScratch(file, {
      pages: [1], engine: 'rapidocr', language: ' en ', dpi: 180, confirmation: 'quote-ocr',
    });
    await api.compareOcrScratch(file, {
      pages: [1], engine: 'rapidocr', language: ' en ', dpi: 180, confirmation: 'quote-ocr',
    });
    await api.estimateTranscribeScratch(file, {
      engine: 'whisper', language: ' fr ', model_size: ' small ', vad: false,
      confirmation: 'quote-transcribe',
    });
    await api.compareTranscribeScratch(file, {
      engine: 'whisper', language: ' fr ', model_size: ' small ', vad: false,
      confirmation: 'quote-transcribe',
    });
    await api.compareTopicSegmentationScratch(
      new File(['sample'], 'sample.txt', { type: 'text/plain' }),
      { variants: [{ id: 'balanced', engine: 'local', settings: { detail: 'balanced' } }], language: ' de ' },
    );

    expect(requests.map(({ input }) => String(input))).toEqual([
      '/api/projects/project%2Fone/ocr/compare-scratch/estimate',
      '/api/projects/project%2Fone/ocr/compare-scratch',
      '/api/projects/project%2Fone/transcribe/compare-scratch/estimate',
      '/api/projects/project%2Fone/transcribe/compare-scratch',
      '/api/projects/project%2Fone/topic-segmentation/compare-scratch',
    ]);
    for (const { init } of requests) {
      expect(init?.method).toBe('POST');
      expect(init?.body).toBeInstanceOf(FormData);
      expect(new Headers(init?.headers).get('content-type')).toBeNull();
    }
    const ocrEstimate = requests[0]?.init?.body as FormData;
    expect([...ocrEstimate.keys()]).toEqual(['file', 'payload']);
    expect(ocrEstimate.get('payload')).toBe(JSON.stringify({
      pages: [1], engine: 'rapidocr', language: 'en', dpi: 180,
    }));
    const ocr = requests[1]?.init?.body as FormData;
    expect([...ocr.keys()]).toEqual(['file', 'payload']);
    expect(ocr.get('file')).toStrictEqual(file);
    expect(ocr.get('payload')).toBe(JSON.stringify({
      pages: [1], engine: 'rapidocr', language: 'en', dpi: 180, confirmation: 'quote-ocr',
    }));
    const transcribeEstimate = requests[2]?.init?.body as FormData;
    expect(transcribeEstimate.get('payload')).toBe(JSON.stringify({
      engine: 'whisper', language: 'fr', model_size: 'small', vad: false,
    }));
    const transcribe = requests[3]?.init?.body as FormData;
    expect([...transcribe.keys()]).toEqual(['file', 'payload']);
    expect(transcribe.get('payload')).toBe(JSON.stringify({
      engine: 'whisper', language: 'fr', model_size: 'small', vad: false,
      confirmation: 'quote-transcribe',
    }));
    const topic = requests[4]?.init?.body as FormData;
    expect([...topic.keys()]).toEqual(['file', 'payload']);
    expect(topic.get('payload')).toBe(JSON.stringify({
      variants: [{ id: 'balanced', engine: 'local', settings: { detail: 'balanced' } }], language: 'de',
    }));
  });

});
