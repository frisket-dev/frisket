// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ProductTelemetryProvider } from '../../src/telemetry/ProductTelemetryProvider';
import { useTelemetryDisclosureReady } from '../../src/telemetry/disclosureReady';

const config = vi.hoisted(() => vi.fn());
vi.mock('../../src/api/open', () => ({ getRuntimeConfig: config }));
vi.mock('../../src/telemetry/productTelemetry', () => ({
  configureProductTelemetry: vi.fn(), sendAppOpened: vi.fn(),
  setTelemetryPreference: vi.fn(), telemetryPreference: () => 'unanswered',
}));
function Probe() { return <output data-testid="ready">{String(useTelemetryDisclosureReady())}</output>; }
beforeEach(() => { config.mockReset(); });
afterEach(cleanup);

describe('first-use telemetry sequencing', () => {
  it('waits for config and the actual disclosure choice', async () => {
    let resolve!: (value: { product_telemetry_available: boolean }) => void;
    config.mockReturnValue(new Promise((done) => { resolve = done; }));
    render(<ProductTelemetryProvider><Probe /></ProductTelemetryProvider>);
    expect(screen.getByTestId('ready')).toHaveTextContent('false');
    await act(async () => resolve({ product_telemetry_available: true }));
    expect(screen.getByTestId('ready')).toHaveTextContent('false');
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(screen.getByTestId('ready')).toHaveTextContent('true');
  });

  it.each(['unavailable', 'request failure'])('allows guidance after %s', async (outcome) => {
    if (outcome === 'unavailable') config.mockResolvedValue({ product_telemetry_available: false });
    else config.mockRejectedValue(new Error('offline'));
    render(<ProductTelemetryProvider><Probe /></ProductTelemetryProvider>);
    await waitFor(() => expect(screen.getByTestId('ready')).toHaveTextContent('true'));
    expect(screen.queryByTestId('product-telemetry-disclosure')).not.toBeInTheDocument();
  });
});
