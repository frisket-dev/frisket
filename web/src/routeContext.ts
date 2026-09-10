const RUN_CONTEXT_KEYS = ['run_id', 'run', 'job_id', 'trace_id'] as const;

type PathSegment = {
  raw: string;
  decoded: string | null;
};

interface RoutePathParts {
  parts: string[];
  malformed: boolean;
}

const safeDecodePathSegment = (segment: string): string | null => {
  try {
    return decodeURIComponent(segment);
  } catch {
    return null;
  }
};

const parsePathSegments = (path: string) => {
  const segments: PathSegment[] = path
    .split('/')
    .filter(Boolean)
    .map((raw) => ({ raw, decoded: safeDecodePathSegment(raw) }));
  return {
    segments,
    parts: segments.map((segment) => segment.decoded ?? segment.raw),
    malformed: segments.some((segment) => segment.decoded === null),
  };
};

export const splitPathForRoute = (path: string): RoutePathParts => {
  const parsed = parsePathSegments(path);
  return {
    // Lossy fallback for route dispatch only. Do not use these parts as
    // identity values without checking that the specific segment decoded.
    parts: parsed.parts,
    malformed: parsed.malformed,
  };
};

export const browserRouteContext = (): Record<string, unknown> => {
  const path = parsePathSegments(window.location.pathname);
  const segments = path.segments;
  const context: Record<string, unknown> = {
    route: window.location.pathname,
    browser: {
      user_agent: navigator.userAgent,
      language: navigator.language,
      viewport: { width: window.innerWidth, height: window.innerHeight },
    },
  };
  if (path.malformed) {
    context.route_parse_error = 'malformed_percent_encoding';
  }
  if (segments[0]?.decoded === 'p' && segments[1]?.decoded) {
    context.project_id = segments[1].decoded;
    if (segments[2]?.decoded === 's' && segments[3]?.decoded) {
      context.sheet_id = segments[3].decoded;
    }
  }
  const params = new URLSearchParams(window.location.search);
  for (const key of RUN_CONTEXT_KEYS) {
    const value = params.get(key);
    if (value && /^[A-Za-z0-9_.:-]{1,120}$/.test(value)) {
      context[key === 'run' ? 'run_id' : key] = value;
    }
  }
  return context;
};
