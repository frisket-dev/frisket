import { defineEditionModule } from './module';
import { TEAM_MODELS_GATEWAY_SECTION } from '../settings/teamModelsGatewaySection';
import {
  LOCAL_EDITION_DESCRIPTOR,
  TEAM_EDITION_DESCRIPTOR,
} from './posture';

export const LOCAL_EDITION_MODULE = defineEditionModule({
  descriptor: LOCAL_EDITION_DESCRIPTOR,
  modelPullScope: 'workspace',
});

export const TEAM_EDITION_MODULE = defineEditionModule({
  descriptor: TEAM_EDITION_DESCRIPTOR,
  modelPullScope: 'organization',
  settingsSections: [TEAM_MODELS_GATEWAY_SECTION],
});
