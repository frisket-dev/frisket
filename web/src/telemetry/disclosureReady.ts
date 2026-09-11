import { createContext, useContext } from 'react';

/** First-use guidance waits for the existing telemetry choice to settle. */
export const TelemetryDisclosureReadyContext = createContext(false);

export function useTelemetryDisclosureReady(): boolean {
  return useContext(TelemetryDisclosureReadyContext);
}
