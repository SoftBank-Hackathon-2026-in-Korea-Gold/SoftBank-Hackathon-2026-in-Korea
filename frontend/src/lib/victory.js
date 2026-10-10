// When to throw the self-healing victory party, and what to say during it. Pure, so App can call it on every render.

const WEB_URL = /^https?:\/\//i

const healedTargets = (run) => Object.values(run.targetState || {}).filter((s) => s?.healed)

/**
 * True when a finished deployment succeeded only because the healer patched it, and the user has not
 * dismissed the celebration for this run yet.
 */
export function shouldCelebrate(run, dismissedIds) {
  if (!run?.id || dismissedIds?.has(run.id)) return false
  // Re-attaching to (replaying) an already finished, healed deployment also passes this check.
  if (run.status !== 'completed') return false
  return (run.patches?.length ?? 0) > 0 || healedTargets(run).length > 0
}

/** Overlay copy: patch count, unique patch categories (first-seen order), and live http(s) URLs in target order. */
export function summarizeHeal(run) {
  const patches = run?.patches || []
  const categories = [...new Set(patches.map((p) => p?.category).filter(Boolean))]
  const urls = (run?.targets || [])
    .map((t) => run.targetState?.[t])
    .filter((s) => s?.phase === 'done' && WEB_URL.test(s.url || ''))
    .map((s) => s.url)
  return { patchCount: patches.length, categories, urls }
}
