// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import type { HttpModelsGatewayStatus } from '../../src/generated/openHttpContracts';
import { ModelsGatewaySettings } from '../../src/engine-selector/ModelsGatewaySettings';
import { LOCAL_EDITION_MODULE, TEAM_EDITION_MODULE } from '../../src/editions/openModules';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('../../src/api/httpContract', () => ({ httpContract: request }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });
const stored: HttpModelsGatewayStatus = {
  schemaVersion: 'frisket.models_gateway.v1', configured: true, source: 'stored',
  origin: 'https://models.test', token_configured: true, token_hint: '...cret',
  authority: 'organization', can_mutate: true,
  environment_names: ['FRISKET_MODELS_URL', 'FRISKET_MODELS_TOKEN'], error: null, probe: null,
};

it('contributes organization gateway Settings structurally only to Team', () => {
  expect(LOCAL_EDITION_MODULE.settingsSections.some((section) => section.id === 'organization.modelsGateway')).toBe(false);
  const section = TEAM_EDITION_MODULE.settingsSections.find((item) => item.id === 'organization.modelsGateway');
  expect(section).toMatchObject({ scope: 'organization', section: 'models-gateway' });
});

it('uses status authority to show read-only environment instructions without a token form', async () => {
  request.mockResolvedValue({ ...stored, source: 'environment', can_mutate: false });
  render(<ModelsGatewaySettings scope="organization" />);
  expect(await screen.findByText(/FRISKET_MODELS_URL/)).toBeInTheDocument();
  expect(screen.queryByLabelText('Gateway token')).not.toBeInTheDocument();
  expect(request).toHaveBeenCalledOnce();
  expect(request).toHaveBeenCalledWith('outer.get_org_models_gateway.get', expect.objectContaining({ signal: expect.any(AbortSignal) }));
});

it('reuses the validated gateway form in Settings and refreshes status after receipt save', async () => {
  request.mockResolvedValueOnce(stored).mockResolvedValueOnce({
    schemaVersion: 'frisket.models_gateway_validation.v1', normalized_origin: stored.origin,
    validation_token: 'settings-receipt', probe: { ok: true, reachable: true, status: 200,
      detail: null, service: 'frisket-models', version: null, engines: [] },
  }).mockResolvedValue(stored);
  render(<ModelsGatewaySettings scope="organization" />);
  expect(await screen.findByLabelText('Gateway URL')).toHaveValue(stored.origin);
  await userEvent.type(screen.getByLabelText('Gateway token'), 'replacement-token');
  await userEvent.click(screen.getByRole('button', { name: 'Test' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled());
  await userEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(4));
  expect(request).toHaveBeenNthCalledWith(3, 'outer.set_org_models_gateway.put', expect.objectContaining({ body: { origin: stored.origin, token: 'replacement-token', validation_token: 'settings-receipt' } }));
  expect(request).toHaveBeenNthCalledWith(4, 'outer.get_org_models_gateway.get', expect.anything());
  expect(screen.getByLabelText('Gateway token')).toHaveValue('');
});
