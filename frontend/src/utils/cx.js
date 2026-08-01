/**
 * Joins conditional classNames into one string, dropping falsy values.
 * Replaces the array-push / template-literal variants that were previously
 * hand-rolled independently at each call site.
 */
export function cx(...args) {
  return args.filter(Boolean).join(' ');
}
