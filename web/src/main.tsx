import { LOCAL_EDITION_MODULE } from './editions/openModules';
import { OpenEditionRoot } from './entries/OpenEditionRoot';
import { mountEdition } from './entries/mount';

mountEdition(LOCAL_EDITION_MODULE, <OpenEditionRoot />);
