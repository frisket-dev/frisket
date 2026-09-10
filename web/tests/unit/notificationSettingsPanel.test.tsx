// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NotificationSettingsPanel } from '../../src/components/NotificationSettingsPanel';
import {
  defineEditionModule,
  EditionModuleProvider,
  type EditionModule,
} from '../../src/editions/module';
import { LOCAL_EDITION_MODULE } from '../../src/editions/openModules';

const projectApi = vi.hoisted(() => ({
  createNotificationChannel: vi.fn(),
  createNotificationRoute: vi.fn(),
  listNotificationChannels: vi.fn(),
  listNotificationDeliveryRequests: vi.fn(),
  listNotificationRoutes: vi.fn(),
  testNotificationRoute: vi.fn(),
  updateNotificationChannel: vi.fn(),
  updateNotificationRoute: vi.fn(),
}));

vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({ projectApi }),
}));

const emailChannel = {
  id: 11,
  kind: 'email',
  name: 'Email alerts',
  enabled: true,
  ownerKind: 'project',
  hasSecret: true,
  config: { to: 'alerts@example.test' },
};
const slackChannel = {
  id: 22,
  kind: 'slack',
  name: 'Newsroom Slack',
  enabled: true,
  ownerKind: 'project',
  hasSecret: true,
  config: { channel_label: '#alerts' },
};
const emailRoute = {
  id: 33,
  name: 'Email route',
  enabled: true,
  ownerKind: 'project',
  channelId: emailChannel.id,
  sourceKind: 'watch',
  sourceRefMatch: { watch_id: 7 },
  deliveryMode: 'immediate',
};
const slackRoute = {
  id: 44,
  name: 'Slack route',
  enabled: true,
  ownerKind: 'project',
  channelId: slackChannel.id,
  sourceKind: 'watch',
  sourceRefMatch: { watch_id: 7 },
  deliveryMode: 'immediate',
};
const emailRequest = {
  id: 55,
  routeId: emailRoute.id,
  channelId: emailChannel.id,
  deliveryKind: 'immediate',
  status: 'sent',
  lastError: null,
};
const slackRequest = {
  id: 66,
  routeId: slackRoute.id,
  channelId: slackChannel.id,
  deliveryKind: 'immediate',
  status: 'sent',
  lastError: null,
};

beforeEach(() => {
  projectApi.listNotificationChannels.mockResolvedValue({
    channels: [emailChannel, slackChannel],
  });
  projectApi.listNotificationRoutes.mockResolvedValue({
    routes: [emailRoute, slackRoute],
  });
  projectApi.listNotificationDeliveryRequests.mockResolvedValue({
    deliveryRequests: [emailRequest, slackRequest],
  });
  projectApi.testNotificationRoute.mockResolvedValue(slackRequest);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllEnvs();
});

describe('NotificationSettingsPanel configurable email', () => {
  it('preserves configurable email by default', async () => {
    renderPanel(LOCAL_EDITION_MODULE);

    expect(await screen.findAllByText('Email alerts · alerts@example.test')).not.toHaveLength(0);
    expect(screen.getByText('Email route')).toBeVisible();
    expect(screen.getByText('immediate request 55')).toBeVisible();
    expect(screen.getByRole('option', { name: 'Email' })).toBeInTheDocument();

    fireEvent.change(screen.getByTestId('notification-channel-kind'), {
      target: { value: 'email' },
    });
    expect(screen.getByLabelText('Email to address')).toBeVisible();
    expect(screen.getByLabelText('Email from address')).toBeVisible();
  });

  it('hides email-owned UI while preserving Slack and route testing', async () => {
    const editionId = 'test-managed-notification-email';
    const edition = defineEditionModule({
      descriptor: {
        id: editionId,
        capabilities: {
        configurableNotificationDestinations: true,
        configurableNotificationEmail: false,
        identity: true,
        team: true,
        },
      },
    });
    renderPanel(edition);

    expect(await screen.findAllByText('Newsroom Slack · #alerts')).not.toHaveLength(0);
    expect(screen.queryByRole('option', { name: 'Email' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Email to address')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Email from address')).not.toBeInTheDocument();
    expect(screen.queryByText('Email alerts · alerts@example.test')).not.toBeInTheDocument();
    expect(screen.queryByText('Email route')).not.toBeInTheDocument();
    expect(screen.queryByText('immediate request 55')).not.toBeInTheDocument();
    expect(screen.getByText('immediate request 66')).toBeVisible();

    const slackRouteRow = screen.getByText('Slack route')
      .closest('[data-testid="notification-route-item"]');
    expect(slackRouteRow).not.toBeNull();
    fireEvent.click(within(slackRouteRow as HTMLElement).getByRole(
      'button',
      { name: 'Create route test request' },
    ));
    await waitFor(() => expect(projectApi.testNotificationRoute).toHaveBeenCalledWith(
      slackRoute.id,
      slackRoute.ownerKind,
    ));
  });
});

function renderPanel(edition: Readonly<EditionModule>) {
  return render(
    <EditionModuleProvider edition={edition}>
      <NotificationSettingsPanel />
    </EditionModuleProvider>,
  );
}
