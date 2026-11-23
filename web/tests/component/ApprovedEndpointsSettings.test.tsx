// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ApprovedEndpoint, ApprovedEndpointsScope } from '../../src/api/approvedEndpoints';
import { ApiError } from '../../src/api/contractErrors';

const { listApprovedEndpoints, replaceApprovedEndpoints } = vi.hoisted(() => ({
  listApprovedEndpoints: vi.fn<(scope: ApprovedEndpointsScope) => Promise<ApprovedEndpoint[]>>(),
  replaceApprovedEndpoints: vi.fn<(scope: ApprovedEndpointsScope, endpoints: ApprovedEndpoint[]) => Promise<ApprovedEndpoint[]>>(),
}));

vi.mock('../../src/api/approvedEndpoints', () => ({ listApprovedEndpoints, replaceApprovedEndpoints }));

import { ApprovedEndpointsSettings } from '../../src/settings/ApprovedEndpointsSettings';

afterEach(cleanup);

const gpu: ApprovedEndpoint = { host: 'gpu.newsroom.example', port: 8000, label: 'Newsroom GPU' };

describe('Approved endpoints editor', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listApprovedEndpoints.mockResolvedValue([gpu]);
    replaceApprovedEndpoints.mockImplementation(async (_scope, endpoints) => endpoints);
  });

  it('lists the explicitly added destinations and says nothing else is added', async () => {
    render(<ApprovedEndpointsSettings scope="workspace" />);
    expect(await screen.findByText('gpu.newsroom.example')).toBeInTheDocument();
    expect(screen.getByText('Newsroom GPU')).toBeInTheDocument();
    expect(screen.getByTestId('approved-endpoints-settings')).toHaveTextContent('Nothing else is added automatically.');
    expect(listApprovedEndpoints).toHaveBeenCalledWith('workspace');
  });

  it('adds a host with an optional port and label by replacing the whole list', async () => {
    render(<ApprovedEndpointsSettings scope="workspace" />);
    await screen.findByText('gpu.newsroom.example');
    fireEvent.change(screen.getByTestId('approved-endpoint-host'), { target: { value: ' search.example.org ' } });
    fireEvent.change(screen.getByTestId('approved-endpoint-label'), { target: { value: 'Search' } });
    fireEvent.click(screen.getByTestId('approved-endpoint-add'));
    await waitFor(() => expect(replaceApprovedEndpoints).toHaveBeenCalledWith('workspace', [
      gpu,
      { host: 'search.example.org', port: null, label: 'Search' },
    ]));
    expect(await screen.findByText('search.example.org')).toBeInTheDocument();
    expect(screen.getByTestId('approved-endpoint-host')).toHaveValue('');
  });

  it('refuses a port outside 1 to 65535 before sending', async () => {
    render(<ApprovedEndpointsSettings scope="workspace" />);
    await screen.findByText('gpu.newsroom.example');
    fireEvent.change(screen.getByTestId('approved-endpoint-host'), { target: { value: 'a.example' } });
    fireEvent.change(screen.getByTestId('approved-endpoint-port'), { target: { value: '70000' } });
    expect(screen.getByTestId('approved-endpoint-add')).toBeDisabled();
  });

  it('removes one entry', async () => {
    render(<ApprovedEndpointsSettings scope="organization" />);
    await screen.findByText('gpu.newsroom.example');
    fireEvent.click(screen.getByRole('button', { name: 'Remove gpu.newsroom.example' }));
    await waitFor(() => expect(replaceApprovedEndpoints).toHaveBeenCalledWith('organization', []));
    expect(await screen.findByText('No approved endpoints')).toBeInTheDocument();
  });

  it('becomes read-only when the organization refuses the change', async () => {
    replaceApprovedEndpoints.mockRejectedValue(new ApiError(403, 'organization owner required'));
    render(<ApprovedEndpointsSettings scope="organization" />);
    await screen.findByText('gpu.newsroom.example');
    fireEvent.click(screen.getByRole('button', { name: 'Remove gpu.newsroom.example' }));
    expect(await screen.findByText('Only organization owners can change approved endpoints.')).toBeInTheDocument();
    expect(screen.queryByTestId('approved-endpoint-add-form')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Remove gpu.newsroom.example' })).toBeNull();
    expect(screen.getByText('gpu.newsroom.example')).toBeInTheDocument();
  });
});
