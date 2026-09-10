/**
 * Error-CLASS remediation. Fix-the-genre seam: this is the ONE place an
 * error's (code, details) get turned into user-facing guidance — no
 * component should string-match a raw message to decide what to show.
 *
 * Three codes are named because they're recoverable: `missing_provider_key`
 * (backend: frisket.runner.MissingProviderKey, blocked at run-confirm time
 * before any row work happens), `ollama_unreachable` (backend:
 * frisket.llm.remediation.classify_llm_error, detected per-row since it's
 * only knowable by actually trying the connection), and
 * `model_not_installed` (same backend seam: the local server answered 404
 * with a machine-readable not-found reason — up, but the model isn't
 * pulled). Everything else keeps its original message untouched.
 */

/** One pydantic field-validation failure, as surfaced by the backend's
 *  `<kind> params did not validate` 400 (frisket.contracts.action_validation
 *  `_validation_error_details`: `{errors: exc.errors(...)}`, each entry a
 *  `{loc, msg, type}` dict). `path` is the dotted `loc` (e.g. `fields.0.type`;
 *  a root-level model_validator failure has an empty `loc` and so an empty
 *  path). */
export interface FieldValidationError {
  path: string;
  message: string;
}

export interface RemediatedError {
  /** The message to show as the primary line. */
  message: string;
  /** Secondary guidance line, when the error class has known remediation. */
  remediation?: string;
  /** True when "Open Diagnose" is a useful next step for this error. */
  showDiagnose?: boolean;
  /** Per-field breakdown of a params-did-not-validate 400, when the backend
   *  attached one — WHICH param failed and why, not just the summary
   *  message. */
  fieldErrors?: FieldValidationError[];
}

/** Pulls the pydantic error list out of an ApiError's `details.errors` (see
 *  `_validation_error_details` in frisket/contracts/actions/validation_helpers.py)
 *  regardless of which `code` the backend picked — every params-did-not-
 *  validate failure carries this shape, named codes and the generic
 *  `invalid_params` fallback alike. */
function fieldErrorsFromDetails(
  details: Record<string, unknown> | undefined,
): FieldValidationError[] | undefined {
  const errors = details?.errors;
  if (!Array.isArray(errors)) return undefined;
  const out: FieldValidationError[] = [];
  for (const entry of errors) {
    if (!entry || typeof entry !== 'object') continue;
    const loc = (entry as Record<string, unknown>).loc;
    const msg = (entry as Record<string, unknown>).msg;
    if (typeof msg !== 'string') continue;
    const path = Array.isArray(loc) ? loc.map((part) => String(part)).join('.') : '';
    out.push({ path, message: msg });
  }
  return out.length > 0 ? out : undefined;
}

/** Anything with a typed backend (code, details) pair — an ApiError instance,
 * or the plain {message, code, details} shape the workspace-chrome toast
 * state carries (WorkspaceToastError). Duck-typed rather than `instanceof
 * ApiError` so this seam works for both without a reconstruction step. */
export interface TypedErrorLike {
  message: string;
  code?: string;
  details?: Record<string, unknown>;
}

function isTypedErrorLike(error: unknown): error is TypedErrorLike {
  return (
    typeof error === 'object' &&
    error !== null &&
    typeof (error as { message?: unknown }).message === 'string'
  );
}

export function remediateApiError(error: unknown): RemediatedError {
  if (!isTypedErrorLike(error)) {
    return { message: error instanceof Error ? error.message : String(error) };
  }
  const provider = typeof error.details?.provider === 'string' ? error.details.provider : undefined;
  const ollamaUrl =
    typeof error.details?.endpoint_origin === 'string' ? error.details.endpoint_origin : undefined;
  const fieldErrors = fieldErrorsFromDetails(error.details);
  switch (error.code) {
    case 'missing_provider_key':
      return {
        message: error.message,
        remediation: provider
          ? `No API key is configured for ${provider}.`
          : 'No API key is configured for this provider.',
        showDiagnose: true,
        fieldErrors,
      };
    case 'ollama_unreachable':
      return {
        message: error.message,
        // Class, not brand: the "ollama" slot serves any OpenAI-compatible
        // local server. Mirrors frisket/llm/remediation.py's canonical copy.
        remediation: ollamaUrl
          ? `No local AI server is responding at ${ollamaUrl} — start Ollama or LM Studio, or pick another model.`
          : 'No local AI server is responding — start Ollama or LM Studio, or pick another model.',
        showDiagnose: true,
        fieldErrors,
      };
    case 'model_not_installed': {
      // The server is up but the model isn't pulled (backend:
      // frisket.llm.remediation.classify_llm_error, typed from the provider's
      // machine-readable 404 reason — never prose-matched). The one concrete
      // fix is a command the user can copy; brand appears only inside it.
      const pullCommand =
        typeof error.details?.pull_command === 'string' ? error.details.pull_command : undefined;
      return {
        message: error.message,
        remediation: pullCommand
          ? `This model isn't installed on your local server — install it there (\`${pullCommand}\`), or pick another model.`
          : "This model isn't installed on your local server — install it there, or pick another model.",
        showDiagnose: true,
        fieldErrors,
      };
    }
    default:
      return { message: error.message, fieldErrors };
  }
}

/** HomeScreen.tsx's boot-time "can't reach the server" failure: the
 * backend can't be in the loop (there's no server to ask), so the
 * remediation is fixed, local copy naming the exact fix. */
export function remediateServerUnreachable(error: Error): RemediatedError {
  return {
    message: `Cannot reach the frisket server: ${error.message}`,
    remediation: 'Start it with `frisket <workspace>` from your project directory.',
  };
}
