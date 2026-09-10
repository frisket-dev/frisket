export const OpenPeek = ({ ctx }) => {
  ctx?.peek?.open('demo.peek_panel.panel.peek');
};

export const PeekPanel = ({ React, ctx }) => {
  return React.createElement('section', {
    'data-testid': 'demo-peek-panel',
    'data-schema': ctx?.schemaVersion ?? 'missing',
    'data-can-close': String(typeof ctx?.peek?.close === 'function'),
  }, React.createElement('button', {
    type: 'button',
    'data-testid': 'demo-peek-close',
    onClick: () => ctx?.peek?.close(),
  }, 'close peek'));
};
