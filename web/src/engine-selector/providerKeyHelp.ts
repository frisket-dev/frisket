export interface ProviderKeyHelp {
  label: string;
  url?: string;
  prefix?: string;
  placeholder?: string;
  billing?: string;
  steps?: readonly string[];
}

const HELP: Readonly<Record<string, ProviderKeyHelp>> = {
  gemini: {
    label: 'Gemini',
    url: 'https://aistudio.google.com',
    prefix: 'AIza',
    placeholder: 'AIza…',
    billing: 'Free tier with rate limits. Higher limits need a Google Cloud billing account.',
    steps: [
      'Sign in with a Google account.',
      'Choose Get API key, then Create API key.',
      'Copy the key. It starts with AIza.',
    ],
  },
  openai: {
    label: 'OpenAI',
    url: 'https://platform.openai.com',
    prefix: 'sk-',
    placeholder: 'sk-…',
    billing: 'Pay as you go. Add a payment method or credits under Billing.',
    steps: [
      'Sign in or create an account.',
      'Open API keys and choose Create new secret key.',
      'Copy the key. It starts with sk- and is shown once.',
    ],
  },
  anthropic: {
    label: 'Anthropic',
    url: 'https://console.anthropic.com',
    prefix: 'sk-ant-',
    placeholder: 'sk-ant-…',
    billing: 'Pay as you go. Buy credits under Billing before your first run.',
    steps: [
      'Sign in or create an account.',
      'Open Settings, then API keys, and choose Create key.',
      'Copy the key. It starts with sk-ant- and is shown once.',
    ],
  },
  openrouter: {
    label: 'OpenRouter',
    url: 'https://openrouter.ai',
    prefix: 'sk-or-',
    placeholder: 'sk-or-…',
    billing: 'Pay as you go. Buy credits before your first run.',
    steps: [
      'Sign in or create an account.',
      'Open Keys and choose Create key.',
      'Copy the key. It starts with sk-or- and is shown once.',
    ],
  },
  datalab: { label: 'Datalab' },
  opendocrouter: { label: 'OpenDocRouter' },
};

export function providerKeyHelp(provider: string): ProviderKeyHelp {
  return HELP[provider] ?? { label: provider };
}

export function providerName(provider: string): string {
  return provider === 'gemini' ? 'Google' : providerKeyHelp(provider).label;
}
