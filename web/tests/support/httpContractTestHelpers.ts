export function jsonResponse(
  payload: unknown,
  status = 200,
  contentType = 'application/json',
): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': contentType },
  });
}
