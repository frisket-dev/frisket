// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { GeneratedActionDraft } from '../../src/api/types';
import { renderApiCallForm } from '../support/apiCallActionFixture';

afterEach(cleanup);
function saved(request: Record<string, unknown>, output = 'response'): GeneratedActionDraft {
  return { action_id: 'map.api_call', scope: { kind: 'sheet_rows', sheet_id: 7 },
    params: { request }, output_names: { api_result: output } };
}
async function run() {
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-run'));
}

describe('API request Params body within the generated host', () => {
  it('keeps an active body visible and editable when switching POST to GET', async () => {
    const onExecute = vi.fn();
    const request = { url: 'https://example.test', method: 'POST', body_mode: 'raw',
      body: 'original body', content_type: 'text/plain' };
    renderApiCallForm({ initialDraft: saved(request), onExecute });
    fireEvent.change(screen.getByTestId('api-call-method'), { target: { value: 'GET' } });
    expect(screen.getByTestId('api-call-body-mode')).toHaveValue('raw');
    expect(screen.getByTestId('api-call-body')).toBeVisible();
    expect(screen.getByTestId('api-call-content-type')).toHaveValue('text/plain');
    fireEvent.change(screen.getByTestId('api-call-body'), { target: { value: 'edited GET body' } });
    await run();
    expect(onExecute.mock.calls[0][0].params.request).toEqual({
      ...request, method: 'GET', body: 'edited GET body',
    });
  });

  it('leaves an ordinary GET request with no body unless explicitly selected', async () => {
    const onExecute = vi.fn();
    const request = { url: 'https://example.test' };
    renderApiCallForm({ initialDraft: saved(request), onExecute });
    expect(screen.getByTestId('api-call-method')).toHaveValue('GET');
    expect(screen.getByTestId('api-call-body-mode')).toHaveValue('none');
    expect(screen.queryByTestId('api-call-body')).not.toBeInTheDocument();
    await run();
    expect(onExecute.mock.calls[0][0].params.request).toEqual(request);
  });

  it.each(['json', 'raw', 'form'])('exposes the saved GET %s body for editing or explicit disabling', async (bodyMode) => {
    const onExecute = vi.fn();
    const request = { url: 'https://example.test', method: 'GET',
      body_mode: bodyMode, body: '{"key":"value"}', form_body: [['key', 'value']] };
    renderApiCallForm({ initialDraft: saved(request), onExecute });
    expect(screen.getByTestId('api-call-body-mode')).toHaveValue(bodyMode);
    expect(screen.getByTestId(bodyMode === 'form' ? 'api-call-form-body-editor' : 'api-call-body')).toBeVisible();
    fireEvent.change(screen.getByTestId('api-call-body-mode'), { target: { value: 'none' } });
    expect(screen.queryByTestId('api-call-body')).not.toBeInTheDocument();
    expect(screen.queryByTestId('api-call-form-body-editor')).not.toBeInTheDocument();
    await run();
    expect(onExecute.mock.calls[0][0].params.request).toEqual({ ...request, body_mode: 'none' });
  });

  it('inserts URL column tokens without surrounding whitespace', () => {
    renderApiCallForm();
    const url = screen.getByTestId('api-call-url') as HTMLTextAreaElement;
    fireEvent.change(url, { target: { value: 'https://example.test/customers/' } });
    url.focus();
    url.setSelectionRange(url.value.length, url.value.length);
    fireEvent.change(screen.getByTestId('api-call-url-insert'), { target: { value: 'customer_id' } });
    expect(url).toHaveValue('https://example.test/customers/{{customer_id}}');
  });
  it('autofills from cURL without changing host-owned output names', async () => {
    const onExecute = vi.fn();
    renderApiCallForm({ onExecute });
    fireEvent.change(screen.getByTestId('field-output-api_result'), { target: { value: 'response' } });
    fireEvent.click(screen.getByTestId('api-call-curl-toggle'));
    fireEvent.change(screen.getByTestId('api-call-curl-input'), { target: {
      value: 'curl https://api.example.com/lookup -X POST -H "Authorization: Bearer xyz" --data \'{"q":"hi"}\'',
    } });
    fireEvent.click(screen.getByTestId('api-call-curl-apply'));
    await run();
    expect(onExecute.mock.calls[0][0]).toMatchObject({
      action_id: 'map.api_call', output_names: { api_result: 'response' },
      params: { request: { method: 'POST', url: 'https://api.example.com/lookup',
        headers: [['Authorization', 'Bearer xyz']], body: '{"q":"hi"}' } },
    });
  });

  it('refuses a malformed cURL import without replacing authored Params', () => {
    renderApiCallForm({ initialDraft: saved({ url: 'https://example.test/original' }) });
    fireEvent.click(screen.getByTestId('api-call-curl-toggle'));
    fireEvent.change(screen.getByTestId('api-call-curl-input'), { target: { value: 'not a curl command' } });
    fireEvent.click(screen.getByTestId('api-call-curl-apply'));
    expect(screen.getByTestId('api-call-curl-error')).toBeVisible();
    expect(screen.getByTestId('api-call-url')).toHaveValue('https://example.test/original');
  });

  it('authors every request field as canonical Params, retaining colons, whitespace and secrets', async () => {
    const onExecute = vi.fn();
    const request = { method: 'PATCH', url: '{{customer_id}}/detail',
      headers: [['X:Mode', '  header  '], ['Authorization', 'Bearer {{secret.TOKEN}}']],
      query_params: [[' filter:kind ', '  query  ']], cookies: [['session', '{{customer_id}}']],
      form_body: [[' field:variant ', '  form  ']], body_mode: 'raw', body: ' raw {{customer_id}} ',
      content_type: 'text/plain', timeout: 12.5, max_requests_per_second: 2.5, follow_redirects: false };
    renderApiCallForm({ initialDraft: saved(request), onExecute, selectedRowIds: ['101', '103'] });
    expect(screen.getByTestId('api-call-headers-name-0')).toHaveValue('X:Mode');
    expect(screen.getByTestId('api-call-headers-value-0')).toHaveValue('  header  ');
    expect(screen.getByTestId('api-call-max-rps')).toHaveValue(2.5);
    fireEvent.change(screen.getByTestId('api-call-query-value-0'), { target: { value: '  edited query  ' } });
    await run();
    expect(onExecute.mock.calls[0][0]).toMatchObject({
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101, 103] },
      params: { request: { ...request, query_params: [[' filter:kind ', '  edited query  ']] } },
    });
    expect(onExecute.mock.calls[0][0].params).not.toHaveProperty('output_name');
  });

  it('edits pairs directly and removes a blank pair explicitly', async () => {
    const onExecute = vi.fn();
    renderApiCallForm({ initialDraft: saved({ url: 'https://example.test' }), onExecute });
    fireEvent.click(screen.getByTestId('api-call-headers-add'));
    fireEvent.change(screen.getByTestId('api-call-headers-name-0'), { target: { value: 'X:Key' } });
    fireEvent.change(screen.getByTestId('api-call-headers-value-0'), { target: { value: '  exact  ' } });
    fireEvent.click(screen.getByTestId('api-call-headers-add'));
    fireEvent.click(screen.getByTestId('api-call-headers-remove-1'));
    await run();
    expect(onExecute.mock.calls[0][0].params.request.headers).toEqual([['X:Key', '  exact  ']]);
  });

  it.each(['{"x":1,"x":2}', '{"x":1e999}', '{'])(
    'shows strict JSON feedback for %s and honors host validation refusal', async (body) => {
      const onExecute = vi.fn();
      renderApiCallForm({ initialDraft: saved({ url: 'https://example.test', method: 'POST',
        body_mode: 'json', body }), onExecute,
      resolveParams: async () => ({ diagnostics: {
        request: { ok: false, message: 'Invalid JSON request body.' },
      }, logical_outputs: [] }) });
      await screen.findByText('Invalid JSON request body.');
      expect(screen.getByTestId('generated-action-run')).toBeDisabled();
      expect(screen.getByTestId('generated-action-preview')).toBeDisabled();
      expect(onExecute).not.toHaveBeenCalled();
    },
  );

  it('does not apply JSON parsing to a raw body and retains inactive saved fields', async () => {
    const onExecute = vi.fn();
    const request = { url: 'https://example.test', method: 'POST', body_mode: 'raw',
      body: '{"x":1,"x":2}', form_body: [['inactive', '{{secret.UNUSED}}']] };
    renderApiCallForm({ initialDraft: saved(request), onExecute });
    await run();
    expect(onExecute.mock.calls[0][0].params.request).toEqual(request);
  });

  it.each(['Missing URL.', 'Invalid request scheme.', 'Header value needs a name.',
    'Request rate produces an infinite pacing interval.', 'Timeout exceeds 120 seconds.'])(
    'keeps authoritative request diagnostics in the host shell: %s', async (message) => {
      const onExecute = vi.fn();
      renderApiCallForm({ initialDraft: saved({ url: 'https://example.test' }), onExecute,
        resolveParams: async () => ({ diagnostics: { request: { ok: false, message } },
          logical_outputs: [] }) });
      await screen.findByText(message);
      expect(screen.getByTestId('generated-action-run')).toBeDisabled();
      expect(onExecute).not.toHaveBeenCalled();
    },
  );

  it('uses host-owned output collision and reservation rules', async () => {
    renderApiCallForm({ initialDraft: saved({ url: 'https://example.test' }, 'customer_id') });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeDisabled());
    expect(screen.getByTestId('field-output-api_result')).toHaveValue('customer_id');
  });

  it('keeps canonical request editing available while another run is active', async () => {
    const onExecute = vi.fn();
    renderApiCallForm({ running: true, initialDraft: saved({ url: 'https://example.test' }), onExecute });
    fireEvent.change(screen.getByTestId('api-call-url'), { target: { value: 'https://example.test/next' } });
    await run();
    expect(onExecute.mock.calls[0][0].params.request.url).toBe('https://example.test/next');
  });
});
