// A Home click's transient navigation intent, never stored as project identity.
let guideArrival: string | null = null;

export function setSampleGuideArrival(projectId: string | null): void {
  guideArrival = projectId;
}

export function consumeSampleGuideArrival(projectId: string): boolean {
  const requested = guideArrival === projectId;
  guideArrival = null;
  return requested;
}
