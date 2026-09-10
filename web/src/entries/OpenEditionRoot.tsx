import App from '../App';
import { useEditionModule } from '../editions/module';

/** The real open product root shared by local, team, and managed composition. */
export function OpenEditionRoot() {
  const edition = useEditionModule();
  return (
    <div data-edition={`frisket-edition:${edition.descriptor.id}`}>
      <App />
    </div>
  );
}
