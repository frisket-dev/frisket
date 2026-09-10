import { OpenEditionRoot } from './OpenEditionRoot';
import { mountEdition } from './mount';
import { TEAM_EDITION_MODULE } from '../editions/openModules';

document.documentElement.dataset.frisketEdition = 'frisket-edition:team';
mountEdition(TEAM_EDITION_MODULE, <OpenEditionRoot />);
