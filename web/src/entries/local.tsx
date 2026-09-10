import { OpenEditionRoot } from './OpenEditionRoot';
import { mountEdition } from './mount';
import { LOCAL_EDITION_MODULE } from '../editions/openModules';

document.documentElement.dataset.frisketEdition = 'frisket-edition:local';
mountEdition(LOCAL_EDITION_MODULE, <OpenEditionRoot />);
