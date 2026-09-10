import { describe, expect, it } from 'vitest';

import { OCR_ENGINE_FALLBACK } from '../../src/actions/engineCatalog';
import { TRANSCRIBE_ENGINE_FALLBACK } from '../../src/actions/transcribeEngineCatalog';

function engineById(
  engines: typeof OCR_ENGINE_FALLBACK | undefined,
  id: string,
) {
  return engines?.find((engine) => engine.id === id);
}

describe('pre-catalog engine fallbacks', () => {
  it('fails closed for optional local and sidecar runtimes', () => {
    const representatives = [
      engineById(OCR_ENGINE_FALLBACK, 'rapidocr'),
      engineById(OCR_ENGINE_FALLBACK, 'paddleocr-vl'),
      engineById(OCR_ENGINE_FALLBACK, 'surya2'),
      engineById(TRANSCRIBE_ENGINE_FALLBACK, 'faster_whisper'),
    ];

    for (const engine of representatives) {
      expect(engine, 'representative fallback engine is missing').toBeDefined();
      expect(engine?.available, engine?.id).toBe(false);
      expect(engine?.error, engine?.id).toBe('Action catalog unavailable');
    }
  });
});
