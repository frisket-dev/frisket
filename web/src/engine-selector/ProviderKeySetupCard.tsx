import '../components/engine-selector/EngineSelector.css';
import { ExternalLink } from 'lucide-react';

import { ProviderKeyForm, type ProviderKeyFormProps } from './ProviderKeyForm';
import { providerKeyHelp } from './providerKeyHelp';

type ProviderKeySetupCardProps<Result> = Omit<
  ProviderKeyFormProps<Result>,
  'provider' | 'providers' | 'compact' | 'keyLabel' | 'keyPlaceholder' | 'keyPrefix'
> & { provider: string };

/** Provider guidance around the shared validate-then-save credential form. */
export function ProviderKeySetupCard<Result>({ provider, ...form }: ProviderKeySetupCardProps<Result>) {
  const help = providerKeyHelp(provider);
  return <section className="selector-setup provider-key-setup-card">
    <h3>Add your {help.label} key</h3>
    <p>An API key works like a password.</p>
    {help.steps && <div className="provider-key-setup-card__steps">
      <strong>How to get a key</strong>
      <ol>{help.steps.map((step) => <li key={step}>{step}</li>)}</ol>
    </div>}
    {help.url && <a href={help.url} target="_blank" rel="noreferrer">
      Open {new URL(help.url).hostname}<ExternalLink size={12} aria-hidden />
    </a>}
    <ProviderKeyForm
      {...form}
      provider={provider}
      compact
      keyLabel="API key"
      keyPlaceholder={help.placeholder}
      keyPrefix={help.prefix}
    />
  </section>;
}
