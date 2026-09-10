import { defineEditionModule } from './module';
import {
  LOCAL_EDITION_DESCRIPTOR,
  TEAM_EDITION_DESCRIPTOR,
} from './posture';

export const LOCAL_EDITION_MODULE = defineEditionModule({
  descriptor: LOCAL_EDITION_DESCRIPTOR,
});

export const TEAM_EDITION_MODULE = defineEditionModule({
  descriptor: TEAM_EDITION_DESCRIPTOR,
});
