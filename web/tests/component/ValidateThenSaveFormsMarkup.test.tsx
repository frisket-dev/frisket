// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ModelsGatewayForm } from '../../src/engine-selector/ModelsGatewayForm';
import { ProviderKeyForm } from '../../src/engine-selector/ProviderKeyForm';
import type { ProviderValidateResult } from '../../src/api/types';

const gateway = vi.hoisted(() => ({ validateModelsGateway: vi.fn(), saveModelsGateway: vi.fn() }));
vi.mock('../../src/api/modelsGateway', () => gateway);
afterEach(() => { cleanup(); vi.resetAllMocks(); });

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });
const markup = (container: HTMLElement) => container.innerHTML;
const button = (name: string) => screen.getByRole('button', { name });
const probe = (ok: boolean, detail: string | null = null) => ({
  schemaVersion: 'frisket.models_gateway_validation.v1', normalized_origin: 'https://models.test/',
  validation_token: 'gateway-receipt', probe: { ok, reachable: ok, status: ok ? 200 : 401, detail, service: null, version: null, engines: [] },
});

describe.each([false, true])('ModelsGatewayForm compact=%s', (compact) => {
  const typeCredentials = () => {
    fireEvent.change(screen.getByLabelText('Gateway URL'), { target: { value: 'https://models.test' } });
    fireEvent.change(screen.getByLabelText('Gateway token'), { target: { value: 'secret-token' } });
  };

  it('pins markup through test, receipt, save and saved', async () => {
    const validation = deferred<ReturnType<typeof probe>>();
    const save = deferred<unknown>();
    gateway.validateModelsGateway.mockReturnValue(validation.promise);
    gateway.saveModelsGateway.mockReturnValue(save.promise);
    const onSaved = vi.fn();
    const { container } = render(<ModelsGatewayForm scope="organization" compact={compact} onSaved={onSaved} />);
    const steps = [markup(container)];
    typeCredentials(); steps.push(markup(container));
    fireEvent.click(button('Test')); steps.push(markup(container));
    validation.resolve(probe(true)); await flush(); steps.push(markup(container));
    fireEvent.click(button('Save')); steps.push(markup(container));
    save.resolve({}); await flush(); steps.push(markup(container));
    expect(onSaved).toHaveBeenCalledOnce();
    expect(steps).toMatchSnapshot();
  });

  it('pins markup for rejected, failed and stale tests and a failed save', async () => {
    const { container } = render(<ModelsGatewayForm scope="workspace" compact={compact} onSaved={vi.fn()} />);
    const steps: string[] = [];
    typeCredentials();
    gateway.validateModelsGateway.mockResolvedValueOnce(probe(false, 'bad secret-token'));
    fireEvent.click(button('Test')); await flush(); steps.push(markup(container));
    gateway.validateModelsGateway.mockRejectedValueOnce(new Error('offline'));
    fireEvent.click(button('Test')); await flush(); steps.push(markup(container));
    const stale = deferred<ReturnType<typeof probe>>();
    gateway.validateModelsGateway.mockReturnValueOnce(stale.promise);
    fireEvent.click(button('Test')); steps.push(markup(container));
    fireEvent.change(screen.getByLabelText('Gateway token'), { target: { value: 'other-token' } }); steps.push(markup(container));
    stale.resolve(probe(true)); await flush(); steps.push(markup(container));
    gateway.validateModelsGateway.mockResolvedValueOnce(probe(true));
    fireEvent.click(button('Test')); await flush(); steps.push(markup(container));
    gateway.saveModelsGateway.mockRejectedValueOnce(new Error('lost'));
    fireEvent.click(button('Save')); await flush(); steps.push(markup(container));
    expect(steps).toMatchSnapshot();
  });

  if (!compact) {
    it('pins markup for recheck', async () => {
      const recheck = deferred<ReturnType<typeof probe>>();
      gateway.validateModelsGateway.mockReturnValueOnce(recheck.promise);
      const { container } = render(<ModelsGatewayForm scope="organization" initialOrigin="https://models.test" onSaved={vi.fn()} />);
      const steps = [markup(container)];
      fireEvent.click(button('Recheck saved gateway')); steps.push(markup(container));
      recheck.resolve(probe(true)); await flush(); steps.push(markup(container));
      gateway.validateModelsGateway.mockResolvedValueOnce(probe(false, 'unreachable'));
      fireEvent.click(button('Recheck saved gateway')); await flush(); steps.push(markup(container));
      expect(gateway.validateModelsGateway).toHaveBeenNthCalledWith(1, 'organization', {}, expect.any(AbortSignal));
      expect(steps).toMatchSnapshot();
    });
  }
});

const accepted: ProviderValidateResult = { provider: 'openai', ok: true, reachable: true, status: 200, detail: null, validation_token: 'key-receipt' };
const providers = [{ id: 'openai', label: 'OpenAI' }, { id: 'anthropic', label: 'Anthropic', policy_fields: [] }];

describe.each([false, true])('ProviderKeyForm compact=%s', (compact) => {
  it('pins markup through test, receipt, save and saved', async () => {
    const validation = deferred<ProviderValidateResult>();
    const saving = deferred<string>();
    const validate = vi.fn().mockReturnValue(validation.promise);
    const save = vi.fn().mockReturnValue(saving.promise);
    const onSaved = vi.fn();
    const { container } = render(<ProviderKeyForm providers={providers} canMutate spendCap compact={compact} keyPrefix="sk-"
      validate={validate} save={save} onSaved={onSaved} />);
    const steps = [markup(container)];
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'pk-wrong' } }); steps.push(markup(container));
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'sk-secret' } });
    fireEvent.change(screen.getByLabelText('Spend cap in US dollars'), { target: { value: '5' } }); steps.push(markup(container));
    fireEvent.click(button('Test')); steps.push(markup(container));
    validation.resolve(accepted); await flush(); steps.push(markup(container));
    fireEvent.click(button('Save')); steps.push(markup(container));
    saving.resolve('saved-result'); await flush(); steps.push(markup(container));
    expect(save).toHaveBeenCalledWith('openai', 'sk-secret', 'key-receipt', 5, expect.any(AbortSignal));
    expect(onSaved).toHaveBeenCalledWith('saved-result');
    expect(steps).toMatchSnapshot();
  });

  it('pins markup for rejected, failed and stale tests, a bad cap and a failed save', async () => {
    const validate = vi.fn();
    const save = vi.fn();
    const { container } = render(<ProviderKeyForm providers={providers} canMutate spendCap compact={compact}
      validate={validate} save={save} onSaved={vi.fn()} />);
    const steps: string[] = [];
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'sk-secret' } });
    validate.mockResolvedValueOnce({ ...accepted, ok: false, validation_token: null, detail: 'rejected sk-secret' });
    fireEvent.click(button('Test')); await flush(); steps.push(markup(container));
    validate.mockRejectedValueOnce(new Error('offline'));
    fireEvent.click(button('Test')); await flush(); steps.push(markup(container));
    const stale = deferred<ProviderValidateResult>();
    validate.mockReturnValueOnce(stale.promise);
    fireEvent.click(button('Test')); steps.push(markup(container));
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'sk-other' } }); steps.push(markup(container));
    stale.resolve(accepted); await flush(); steps.push(markup(container));
    validate.mockResolvedValueOnce(accepted);
    fireEvent.click(button('Test')); await flush(); steps.push(markup(container));
    fireEvent.change(screen.getByLabelText('Spend cap in US dollars'), { target: { value: '-1' } });
    fireEvent.click(button('Save')); await flush(); steps.push(markup(container));
    expect(save).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('Spend cap in US dollars'), { target: { value: '' } });
    save.mockRejectedValueOnce(new Error('lost'));
    fireEvent.click(button('Save')); await flush(); steps.push(markup(container));
    fireEvent.change(screen.getByLabelText('Provider'), { target: { value: 'anthropic' } }); steps.push(markup(container));
    expect(steps).toMatchSnapshot();
  });

  it('pins read-only markup', () => {
    const { container } = render(<ProviderKeyForm provider="openai" canMutate={false} compact={compact}
      validate={vi.fn()} save={vi.fn()} onSaved={vi.fn()} />);
    expect(markup(container)).toMatchSnapshot();
  });
});
