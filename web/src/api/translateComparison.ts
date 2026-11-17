import { httpContract } from './httpContract';
import type {
  TranslateCompareScratchInput,
  TranslateCompareScratchResult,
} from './types';

interface TranslateComparisonOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;

interface TranslateComparisonApi {
  compareTranslateScratch(
    projectId: string,
    input: TranslateCompareScratchInput,
    options?: TranslateComparisonOptions,
  ): Promise<TranslateCompareScratchResult>;
}

// This route's public response is the server envelope. Keep the adapter
// transport-only so warning/error extensions remain untouched.
export function createTranslateComparisonApi(
  errorFactory: ContractErrorFactory,
): TranslateComparisonApi {
  return {
    compareTranslateScratch(projectId, input, options = {}) {
      const language = input.language?.trim();
      return httpContract(
        'tenant.translate_compare_scratch.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: {
            engines: input.engines,
            text: input.text,
            target_language: input.targetLanguage?.trim() || 'English',
            language: language ? [language] : null,
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}
