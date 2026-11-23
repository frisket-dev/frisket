import type { AllowedServices } from './api/types';

/** Strictest first: a later value allows more. */
export const ALLOWED_SERVICES_ORDER: readonly AllowedServices[] = [
  'this_machine_only',
  'approved_services',
  'all_services',
];

export const ALLOWED_SERVICES_LABELS: Record<AllowedServices, string> = {
  this_machine_only: 'This machine only',
  approved_services: 'Approved services',
  all_services: 'All services',
};

export const ALLOWED_SERVICES_DESCRIPTIONS: Record<AllowedServices, string> = {
  this_machine_only: 'Use models and tools running on the same computer or server as Frisket.',
  approved_services: 'Also use specific servers and services you or your organization have approved.',
  all_services: 'Use any available AI provider, web service, or local tool.',
};

export const SERVICE_NOT_ALLOWED = 'service_not_allowed';

export function isLooserAllowedServices(current: AllowedServices, next: AllowedServices): boolean {
  return ALLOWED_SERVICES_ORDER.indexOf(next) > ALLOWED_SERVICES_ORDER.indexOf(current);
}

/** The label a restricted project shows on its card and header; null for
 *  All services, which shows nothing. */
export function restrictedServicesLabel(value: AllowedServices | undefined): string | null {
  return value === undefined || value === 'all_services' ? null : ALLOWED_SERVICES_LABELS[value];
}
