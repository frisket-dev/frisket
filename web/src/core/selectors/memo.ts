// A tiny last-call memoize-by-args helper for pure selectors whose output is
// an object. Selectors take DATA as input, never closures, so their inputs are stable
// values (primitives, or object/array references that only change when the
// underlying data actually changes) — that is what makes reference-equality
// memoization sound here. Single-entry cache: if every positional arg is
// `Object.is`-equal to the previous call's, the previous output object is
// returned instead of allocating a new one.

export function memoizeByArgs<Args extends readonly unknown[], R>(
  compute: (...args: Args) => R,
): (...args: Args) => R {
  let lastArgs: Args | null = null;
  let lastResult: R;
  return (...args: Args): R => {
    if (
      lastArgs !== null &&
      lastArgs.length === args.length &&
      lastArgs.every((value, index) => Object.is(value, args[index]))
    ) {
      return lastResult;
    }
    lastArgs = args;
    lastResult = compute(...args);
    return lastResult;
  };
}
