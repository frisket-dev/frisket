import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import EditorModule from 'react-simple-code-editor';
// prism-core sets up the Prism instance the component files register against;
// importing the language files then attaches their grammars to it.
import { highlight, languages } from 'prismjs/components/prism-core';
import 'prismjs/components/prism-clike';
import 'prismjs/components/prism-python';

// react-simple-code-editor ships CJS without an __esModule flag, so under the
// bundler's interop the default import can arrive as the module namespace.
const Editor = (EditorModule as unknown as { default?: typeof EditorModule }).default ?? EditorModule;

/**
 * Reusable code field: an editable textarea with Prism syntax highlighting
 * (via react-simple-code-editor), wrapped in the panel's dark editor chrome —
 * an optional contract bar above and status bar below. The inner textarea
 * carries `textareaTestId` so form/e2e access stays stable.
 */
export function CodeEditor({
  value,
  onValueChange,
  language = 'python',
  ariaLabel,
  textareaTestId,
  editorTestId,
  contract,
  status,
  minRows = 7,
}: {
  value: string;
  onValueChange(value: string): void;
  language?: 'python';
  ariaLabel?: string;
  textareaTestId?: string;
  editorTestId?: string;
  contract?: ReactNode;
  status?: ReactNode;
  minRows?: number;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const grammar = languages[language] ?? languages.clike;

  // react-simple-code-editor doesn't forward arbitrary props to its textarea,
  // so stamp the testid on it once via the container ref.
  useEffect(() => {
    if (!textareaTestId) return;
    const textarea = containerRef.current?.querySelector('textarea');
    if (textarea && textarea.getAttribute('data-testid') !== textareaTestId) {
      textarea.setAttribute('data-testid', textareaTestId);
    }
  }, [textareaTestId]);

  return (
    <div
      className="code-editor"
      data-testid={editorTestId}
      data-language={language}
      ref={containerRef}
    >
      {contract && <div className="code-editor-contract">{contract}</div>}
      <Editor
        value={value}
        onValueChange={onValueChange}
        highlight={(code) => highlight(code, grammar, language)}
        padding={12}
        textareaClassName="code-editor-input"
        preClassName="code-editor-pre"
        tabSize={4}
        insertSpaces
        aria-label={ariaLabel}
        style={{ minHeight: `${minRows * 1.75}em`, fontFamily: 'inherit', fontSize: 'inherit' }}
      />
      {status && <div className="code-editor-status">{status}</div>}
    </div>
  );
}
