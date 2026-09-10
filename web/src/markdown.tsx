// Markdown rendering for cells whose column carries format='markdown'. The grid
// uses Glide's native Markdown cell for the in-grid preview overlay; the row
// drawer (and anything else DOM-side) renders
// through MarkdownView below. `marked` is already a dependency (Glide's
// Markdown cell uses it too), so both halves agree on the dialect.

import DOMPurify from 'dompurify';
import { marked } from 'marked';
import { createElement, useMemo, type ReactNode } from 'react';
import { decodeEscapedText } from './displayText';

// Links in sanitized output open in a new tab (DOMPurify hook runs on every
// sanitize pass; setting attributes here is safe — the node already passed).
const purifierGlobal = globalThis as typeof globalThis & {
  __frisketMarkdownLinkHookInstalled?: boolean;
};

if (!purifierGlobal.__frisketMarkdownLinkHookInstalled) {
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A' && node.hasAttribute('href')) {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
    }
  });
  purifierGlobal.__frisketMarkdownLinkHookInstalled = true;
}

/**
 * Markdown → sanitized React nodes. Cell values are untrusted (imported CSVs,
 * model output). Sanitization is DOMPurify — NOT a hand-rolled scrub: a
 * regex-based approach is bypassable (a tab inside the javascript: scheme
 * survives HTML URL parsing). Keep the sanitizer, then translate its DOM
 * fragment into React nodes instead of injecting sanitized strings.
 */
function renderMarkdown(src: string): ReactNode {
  const html = marked.parse(src) as string;
  return sanitizeToReact(html);
}

function renderHtml(src: string): ReactNode {
  return sanitizeToReact(src);
}

function sanitizeToReact(src: string): ReactNode {
  const fragment = DOMPurify.sanitize(src, {
    FORBID_TAGS: ['style', 'form'],
    FORCE_BODY: true,
    USE_PROFILES: { html: true },
    RETURN_DOM_FRAGMENT: true,
  });
  return domNodesToReact(fragment.childNodes, 'root');
}

function domNodesToReact(nodes: NodeListOf<ChildNode>, keyPrefix: string): ReactNode[] {
  return Array.from(nodes, (node, index) => domNodeToReact(node, `${keyPrefix}-${index}`));
}

function domNodeToReact(node: ChildNode, key: string): ReactNode {
  if (node.nodeType === Node.TEXT_NODE) {
    return node.textContent ?? '';
  }
  if (node.nodeType !== Node.ELEMENT_NODE) {
    return null;
  }

  const element = node as Element;
  const tagName = element.localName;
  const props = attributesToReactProps(element);
  const children = domNodesToReact(element.childNodes, key);

  return createElement(tagName, { ...props, key }, children.length > 0 ? children : undefined);
}

function attributesToReactProps(element: Element): Record<string, unknown> {
  const props: Record<string, unknown> = {};
  for (const attr of Array.from(element.attributes)) {
    const reactName = reactAttributeName(attr.name);
    if (!reactName) continue;
    if (reactName === 'style') {
      props.style = styleStringToReactObject(attr.value);
    } else {
      props[reactName] = attr.value;
    }
  }
  return props;
}

function reactAttributeName(name: string): string | null {
  const lower = name.toLowerCase();
  if (lower.startsWith('on')) return null;
  switch (lower) {
    case 'class':
      return 'className';
    case 'for':
      return 'htmlFor';
    case 'colspan':
      return 'colSpan';
    case 'rowspan':
      return 'rowSpan';
    case 'datetime':
      return 'dateTime';
    case 'srcset':
      return 'srcSet';
    case 'tabindex':
      return 'tabIndex';
    default:
      return lower;
  }
}

function styleStringToReactObject(style: string): Record<string, string> {
  const declarations: Record<string, string> = {};
  for (const declaration of style.split(';')) {
    const separator = declaration.indexOf(':');
    if (separator < 1) continue;
    const name = declaration.slice(0, separator).trim();
    const value = declaration.slice(separator + 1).trim();
    if (!name || !value) continue;
    declarations[cssPropertyToReactName(name)] = value;
  }
  return declarations;
}

function cssPropertyToReactName(name: string): string {
  if (name.startsWith('--')) return name;
  if (name.startsWith('-ms-')) return camelCaseCssName(name.slice(1));
  if (name.startsWith('-')) {
    const vendorName = camelCaseCssName(name.slice(1));
    return vendorName.charAt(0).toUpperCase() + vendorName.slice(1);
  }
  return camelCaseCssName(name);
}

function camelCaseCssName(name: string): string {
  return name.replace(/-([a-z])/g, (_, char: string) => char.toUpperCase());
}

export interface MarkdownViewProps {
  source: string;
  className?: string;
  testId?: string;
  decodeEscapes?: boolean;
}

/** Rendered markdown block (row drawer / detail surfaces). */
export function MarkdownView({ source, className, testId, decodeEscapes }: MarkdownViewProps) {
  const renderedSource = decodeEscapes ? decodeEscapedText(source) : source;
  const content = useMemo(() => renderMarkdown(renderedSource), [renderedSource]);
  return (
    <div
      className={`markdown-body${className ? ` ${className}` : ''}`}
      data-testid={testId}
    >
      {content}
    </div>
  );
}

export function HtmlView({ source, className, testId, decodeEscapes }: MarkdownViewProps) {
  const renderedSource = decodeEscapes ? decodeEscapedText(source) : source;
  const content = useMemo(() => renderHtml(renderedSource), [renderedSource]);
  return (
    <div
      className={`html-body${className ? ` ${className}` : ''}`}
      data-testid={testId}
    >
      {content}
    </div>
  );
}
