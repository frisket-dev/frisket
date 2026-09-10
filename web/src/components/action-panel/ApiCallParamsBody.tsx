import { useState } from 'react';
import { ClipboardPaste, Plus, Trash2 } from 'lucide-react';
import { API_CALL_MAX_TIMEOUT_SECONDS } from '../../actions/apiCallValidation';
import { DuplicateJsonKeyError, parseStrictJson } from '../../actions/strictJson';
import type { SheetMeta } from '../../api/open';
import type { GeneratedActionParams } from '../../generated/actionTypes';
import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';
import { parseCurl } from './parseCurl';
import { PanelSelect } from '../PanelSelect';
import { TemplateComposer } from './SourceInputControl';
import './api-call-form.css';
type HttpRequest = GeneratedActionParams['map.api_call']['request'];
type ApiBodyMode = HttpRequest['body_mode'];
const HTTP_METHODS: HttpRequest['method'][] = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'];
const BODY_MODES: ApiBodyMode[] = ['none', 'form', 'json', 'raw'];
interface PairEditorProps {
  id: string;
  label: string;
  entries: [string, string][];
  columns: SheetMeta['columns'];
  onChange(entries: [string, string][]): void;
  namePlaceholder?: string;
  valuePlaceholder?: string;
}

/** Structured pair editing is deliberate: HTTP query/form names may contain
 * colons, and their values may carry significant leading/trailing whitespace.
 * A line-oriented `Name: value` codec cannot round-trip both without inventing
 * an escaping language. Keep the authored pair shape intact all the way to the
 * translator, while retaining template highlighting/insertion for values. */
function PairEditor({
  id,
  label,
  entries,
  columns,
  onChange,
  namePlaceholder = 'Name',
  valuePlaceholder = 'Value',
}: PairEditorProps) {
  const update = (index: number, key: 0 | 1, value: string) => {
    onChange(entries.map((entry, entryIndex) => (
      entryIndex === index ? (key === 0 ? [value, entry[1]] : [entry[0], value]) as [string, string] : entry
    )));
  };

  return (
    <div className="field-group api-pair-editor" data-testid={`${id}-editor`}>
      <div className="api-pair-heading">
        <span className="field-labels">{label}</span>
        <button
          type="button"
          className="mini-btn"
          data-testid={`${id}-add`}
          onClick={() => onChange([...entries, ['', '']])}
        >
          <Plus size={12} /> Add
        </button>
      </div>
      {entries.length === 0 ? (
        <span className="form-hint">None</span>
      ) : entries.map((entry, index) => (
        <div className="api-pair-row" key={`${id}-${index}`}>
          <input
            className="form-input"
            aria-label={`${label} ${index + 1} name`}
            data-testid={`${id}-name-${index}`}
            placeholder={namePlaceholder}
            value={entry[0]}
            onChange={(event) => update(index, 0, event.target.value)}
          />
          <TemplateComposer
            value={entry[1]}
            onChange={(value) => update(index, 1, value)}
            columns={columns}
            knownTokenPrefixes={['secret.']}
            hideHint
            tightInsert
            compactInsert
            textareaTestId={`${id}-value-${index}`}
            insertTestId={`${id}-value-insert-${index}`}
            ariaLabel={`${label} ${index + 1} value`}
            placeholder={valuePlaceholder}
          />
          <button
            type="button"
            className="icon-btn api-pair-remove"
            aria-label={`Remove ${label.toLowerCase()} ${index + 1}`}
            data-testid={`${id}-remove-${index}`}
            onClick={() => onChange(entries.filter((_, entryIndex) => entryIndex !== index))}
          >
            <Trash2 size={13} />
          </button>
        </div>
      ))}
    </div>
  );
}

/** Edits canonical request Params only. The host owns naming and lifecycle. */
export function ApiCallParamsBody({ params, setParams, sheet }:
  GeneratedActionParamsBodyProps<'map.api_call'>) {
  const request = params.request ?? {} as HttpRequest;
  const update = (patch: Partial<HttpRequest>) => setParams({
    ...params, request: { ...request, ...patch },
  });
  const method = request.method ?? 'GET';
  const url = request.url ?? '';
  const headers = request.headers ?? [];
  const queryParams = request.query_params ?? [];
  const cookies = request.cookies ?? [];
  const formBody = request.form_body ?? [];
  const bodyMode = request.body_mode ?? 'none';
  const body = request.body ?? '';
  const contentType = request.content_type ?? '';
  const timeout = request.timeout ?? 30;
  const maxRequestsPerSecond = request.max_requests_per_second ?? '';
  const followRedirects = request.follow_redirects ?? true;
  const setMethod = (value: HttpRequest['method']) => update({ method: value });
  const setUrl = (value: string) => update({ url: value });
  const setHeaders = (value: [string, string][]) => update({ headers: value });
  const setQueryParams = (value: [string, string][]) => update({ query_params: value });
  const setCookies = (value: [string, string][]) => update({ cookies: value });
  const setFormBody = (value: [string, string][]) => update({ form_body: value });
  const setBodyMode = (value: ApiBodyMode) => update({ body_mode: value });
  const setBody = (value: string) => update({ body: value });
  const setContentType = (value: string) => update({ content_type: value || null });
  const setTimeoutValue = (value: string) => update({ timeout: Number(value) });
  const setMaxRequestsPerSecond = (value: string) => update({
    max_requests_per_second: value.trim() ? Number(value) : null,
  });
  const setFollowRedirects = (value: boolean) => update({ follow_redirects: value });
  const [curlOpen, setCurlOpen] = useState(false);
  const [curlText, setCurlText] = useState('');
  const [curlError, setCurlError] = useState<string | null>(null);
  const applyCurl = () => {
    let parsed: ReturnType<typeof parseCurl>;
    try { parsed = parseCurl(curlText); } catch { parsed = null; }
    if (!parsed) {
      setCurlError('Could not read that as a cURL command with a URL.');
      return;
    }
    const toPairs = (values: {name: string; value: string}[]): [string, string][] =>
      values.map(({ name, value }) => [name, value]);
    update({
      method: parsed.method ?? 'GET', url: parsed.url,
      headers: toPairs(parsed.headers), cookies: toPairs(parsed.cookies),
      query_params: [], form_body: toPairs(parsed.formBody ?? []),
      body_mode: parsed.bodyMode ?? 'none', body: parsed.body ?? '',
      content_type: parsed.contentType ?? null, follow_redirects: parsed.followRedirects,
    });
    setCurlError(null); setCurlText(''); setCurlOpen(false);
  };
  let jsonError: string | null = null;
  if (bodyMode === 'json') {
    try { parseStrictJson(body); } catch (error) {
      jsonError = error instanceof DuplicateJsonKeyError
        ? 'JSON object keys must be unique, including inside nested objects.'
        : 'Enter a valid JSON body. Put templates inside JSON strings.';
    }
  }
  return (
      <div className="api-action-body">
        <div className="api-curl-import">
          <button
            type="button"
            className="mini-btn api-curl-toggle"
            data-testid="api-call-curl-toggle"
            aria-expanded={curlOpen}
            onClick={() => setCurlOpen((open) => !open)}
          >
            <ClipboardPaste size={13} /> Paste a cURL command
          </button>
          {curlOpen && (
            <div className="api-curl-panel">
              <textarea
                className="form-input form-textarea api-curl-textarea"
                value={curlText}
                placeholder="curl 'https://api.example.com/...' -H 'Authorization: Bearer ...'"
                data-testid="api-call-curl-input"
                aria-label="cURL command"
                aria-invalid={Boolean(curlError)}
                aria-describedby={curlError ? 'api-call-curl-error' : undefined}
                onChange={(event) => {
                  setCurlText(event.target.value);
                  if (curlError) setCurlError(null);
                }}
              />
              <div className="api-curl-actions">
                <button
                  type="button"
                  className="btn btn-secondary"
                  data-testid="api-call-curl-apply"
                  disabled={!curlText.trim()}
                  onClick={applyCurl}
                >
                  Autofill from cURL
                </button>
                {curlError && (
                  <span
                    id="api-call-curl-error"
                    className="form-error"
                    data-testid="api-call-curl-error"
                    role="alert"
                  >
                    {curlError}
                  </span>
                )}
              </div>
            </div>
          )}
        </div>
        <label className="field-group api-url-row">
          <span className="field-labels">URL</span>
          <TemplateComposer
            value={url}
            onChange={setUrl}
            columns={(sheet?.columns ?? [])}
            knownTokenPrefixes={['secret.']}
            hideHint
            tightInsert
            textareaTestId="api-call-url"
            insertTestId="api-call-url-insert"
            ariaLabel="Request URL"
            placeholder="https://api.example.com/items/{{id}}"
          />
        </label>
        <span className="form-hint api-template-hint">
          Use {'{{column}}'} for row values and {'{{secret.NAME}}'} for project secrets.
        </span>
        <div className="api-method-row">
          <label className="field-group api-method-field">
            <span className="field-labels">Method</span>
            <PanelSelect
              className="form-input"
              value={method}
              data-testid="api-call-method"
              onChange={(event) => setMethod(event.target.value as HttpRequest['method'])}
            >
              {HTTP_METHODS.map((option) => <option key={option}>{option}</option>)}
            </PanelSelect>
          </label>
        </div>

        <PairEditor
          id="api-call-headers"
          label="Headers"
          entries={headers}
          columns={(sheet?.columns ?? [])}
          onChange={setHeaders}
          valuePlaceholder="Header value"
        />
        <PairEditor
          id="api-call-query"
          label="Query parameters"
          entries={queryParams}
          columns={(sheet?.columns ?? [])}
          onChange={setQueryParams}
          namePlaceholder="Parameter"
          valuePlaceholder="Parameter value"
        />

        <>
            <label className="field-group">
              <span className="field-labels">Body</span>
              <PanelSelect
                className="form-input"
                value={bodyMode}
                data-testid="api-call-body-mode"
                onChange={(event) => setBodyMode(event.target.value as ApiBodyMode)}
              >
                {BODY_MODES.map((option) => <option key={option} value={option}>{option}</option>)}
              </PanelSelect>
            </label>
            {bodyMode === 'form' && (
              <PairEditor
                id="api-call-form-body"
                label="Form fields"
                entries={formBody}
                columns={(sheet?.columns ?? [])}
                onChange={setFormBody}
                namePlaceholder="Field"
                valuePlaceholder="Field value"
              />
            )}
            {(bodyMode === 'json' || bodyMode === 'raw') && (
              <label className="field-group">
                <span className="field-labels">{bodyMode === 'json' ? 'JSON body' : 'Raw body'}</span>
                <TemplateComposer
                  value={body}
                  onChange={setBody}
                  columns={(sheet?.columns ?? [])}
                  knownTokenPrefixes={['secret.']}
            hideHint
            tightInsert
                  textareaTestId="api-call-body"
                  insertTestId="api-call-body-insert"
                  ariaLabel={bodyMode === 'json' ? 'JSON body' : 'Raw body'}
                  placeholder={'{"customer":"{{customer_id}}"}'}
                />
                {jsonError && <span className="form-error" role="alert">{jsonError}</span>}
              </label>
            )}
            {bodyMode === 'raw' && (
              <label className="field-group">
                <span className="field-labels">Content-Type</span>
                <input
                  className="form-input"
                  value={contentType}
                  placeholder="text/plain"
                  data-testid="api-call-content-type"
                  onChange={(event) => setContentType(event.target.value)}
                />
              </label>
            )}
        </>

        <details className="api-advanced" data-testid="api-call-advanced">
          <summary>Advanced</summary>
          <div className="api-advanced-body">
            <PairEditor
              id="api-call-cookies"
              label="Cookies"
              entries={cookies}
              columns={(sheet?.columns ?? [])}
              onChange={setCookies}
              namePlaceholder="Cookie"
              valuePlaceholder="Cookie value"
            />
            <div className="api-number-row">
              <label className="field-group">
                <span className="field-labels">Timeout (seconds)</span>
                <input
                  className="form-input"
                  type="number"
                  min="0.1"
                  max={API_CALL_MAX_TIMEOUT_SECONDS}
                  step="0.1"
                  value={timeout}
                  data-testid="api-call-timeout"
                  onChange={(event) => setTimeoutValue(event.target.value)}
                />
              </label>
              <label className="field-group">
                <span className="field-labels">Max requests / second</span>
                <input
                  className="form-input"
                  type="number"
                  min="0.01"
                  step="any"
                  value={maxRequestsPerSecond}
                  placeholder="Unlimited"
                  data-testid="api-call-max-rps"
                  onChange={(event) => setMaxRequestsPerSecond(event.target.value)}
                />
              </label>
            </div>
            <label className="api-checkbox-row">
              <input
                type="checkbox"
                checked={followRedirects}
                data-testid="api-call-follow-redirects"
                onChange={(event) => setFollowRedirects(event.target.checked)}
              />
              <span>Follow redirects</span>
            </label>
          </div>
        </details>

        <div className="api-egress-note">
          One external HTTP request is sent for each row in the run scope below.
          Mutating requests may be sent again if a run is retried.
        </div>
      </div>

  );
}
