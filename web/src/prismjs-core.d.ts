// The `prism-core` entry (used with react-simple-code-editor) has no bundled
// types; the component side-effect imports register languages against it.
declare module 'prismjs/components/prism-core' {
  export const languages: Record<string, unknown>;
  export function highlight(text: string, grammar: unknown, language: string): string;
}
