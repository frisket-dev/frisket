import { httpContract, type RequestOptions } from './httpContract';
import type {
  TranslateCompareScratchInput,
  TranslateCompareScratchResult,
} from './types';

// This route's public response is the server envelope. Keep the adapter
// transport-only so warning/error extensions remain untouched.
export function createTranslateComparisonApi(
  projectId: string,
) {
  return {
    async compareTranslateScratch(
      input: TranslateCompareScratchInput,
      options: RequestOptions = {},
    ): Promise<TranslateCompareScratchResult> {
      const language = input.language?.trim();
      return httpContract(
        'tenant.translate_compare_scratch.post',
        {
          pathParams: { pid: projectId },
          body: {
            engines: input.engines,
            text: input.text,
            target_language: input.targetLanguage?.trim() || 'English',
            language: language ? [language] : null,
          },
          signal: options.signal,
        },
      );
    },
  };
}
