// Palette command handler: invoked with an invocation-scoped context snapshot
// only when the user clicks the command in the palette.
export const HelloCommand = ({ ctx }) => {
  document.body.dataset.demoCommandRan = ctx?.commandId ?? 'missing';
  document.body.dataset.demoCommandSchema = ctx?.schemaVersion ?? 'missing';
};
