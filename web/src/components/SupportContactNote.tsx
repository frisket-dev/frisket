/**
 * Operator support-contact slot for error surfaces. The `support_contact` field
 * itself is owned by the org schema (display_name, welcome_message,
 * support_contact — hosted/schema.py). This component is only the SLOT: it
 * reads whatever a caller passes optionally and renders nothing until that
 * value exists. No schema, fetch, or global store is introduced here.
 */
export function SupportContactNote({
  supportContact,
}: {
  supportContact?: string | null;
}) {
  const text = supportContact?.trim();
  if (!text) return null;
  return (
    <div className="support-contact-note" data-testid="support-contact-note">
      {text}
    </div>
  );
}
