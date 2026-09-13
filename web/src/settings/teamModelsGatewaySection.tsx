import type { EditionSettingsSection } from './openSettingsRegistry';
import { ModelsGatewaySettings } from '../engine-selector/ModelsGatewaySettings';

export const TEAM_MODELS_GATEWAY_SECTION: EditionSettingsSection = {
  id: 'organization.modelsGateway', scope: 'organization', section: 'models-gateway',
  routePattern: '/settings/organization/models-gateway', title: 'Models gateway',
  navGroup: 'Organization', searchLabels: ['models', 'gateway', 'server', 'token'],
  visibility: 'hosted-only', permission: 'organization-admin',
  component: 'organization.modelsGateway', summary: 'Attach your Frisket models server.',
  handler: () => <ModelsGatewaySettings scope="organization" />,
};
