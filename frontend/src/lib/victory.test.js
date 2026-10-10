import { describe, it, expect } from 'vitest'
import { shouldCelebrate, summarizeHeal } from './victory'

const target = (over = {}) => ({ phase: 'done', url: null, attempts: 0, message: null, healed: false, ...over })

const makeRun = (over = {}) => ({
  id: 'abc123',
  status: 'completed',
  targets: ['local'],
  targetState: { local: target({ url: 'https://a.example' }) },
  patches: [],
  ...over,
})

const patch = (attempt, category) => ({ attempt, category, source: 'rule', rationale: 'fix', target: 'local' })

describe('shouldCelebrate', () => {
  it('returns false for a null or undefined run', () => {
    expect(shouldCelebrate(null, null)).toBe(false)
    expect(shouldCelebrate(undefined, new Set(['x']))).toBe(false)
  })

  it('returns false for an idle run without an id', () => {
    // Arrange
    const run = makeRun({ id: null, status: 'idle', patches: [patch(1, 'deps')] })

    // Act
    const result = shouldCelebrate(run, null)

    // Assert
    expect(result).toBe(false)
  })

  it('returns false while the run is still running even if patches exist', () => {
    const run = makeRun({ status: 'running', patches: [patch(1, 'deps')] })

    expect(shouldCelebrate(run, null)).toBe(false)
  })

  it('returns false for a failed run with patches', () => {
    const run = makeRun({ status: 'failed', patches: [patch(1, 'deps')] })

    expect(shouldCelebrate(run, null)).toBe(false)
  })

  it('returns false for a partial_failure run with patches', () => {
    const run = makeRun({ status: 'partial_failure', patches: [patch(1, 'deps')] })

    expect(shouldCelebrate(run, null)).toBe(false)
  })

  it('returns false for a cancelled run with patches', () => {
    const run = makeRun({ status: 'cancelled', patches: [patch(1, 'deps')] })

    expect(shouldCelebrate(run, null)).toBe(false)
  })

  it('returns false for a completed run that needed no healing', () => {
    const run = makeRun()

    expect(shouldCelebrate(run, null)).toBe(false)
  })

  it('returns true for a completed run with at least one patch', () => {
    const run = makeRun({ patches: [patch(1, 'port')] })

    expect(shouldCelebrate(run, null)).toBe(true)
  })

  it('returns true when a target reports healed even without patch events', () => {
    const run = makeRun({ targetState: { local: target({ healed: true }) } })

    expect(shouldCelebrate(run, null)).toBe(true)
  })

  it('returns false when the run id matches the dismissed id', () => {
    const run = makeRun({ patches: [patch(1, 'port')] })

    expect(shouldCelebrate(run, new Set(['abc123']))).toBe(false)
  })

  it('returns true when a different run was dismissed earlier', () => {
    const run = makeRun({ patches: [patch(1, 'port')] })

    expect(shouldCelebrate(run, new Set(['older-run']))).toBe(true)
  })

  it('keeps earlier runs dismissed after another celebration closes', () => {
    const first = makeRun({ patches: [patch(1, 'port')] })
    const second = makeRun({ id: 'second', patches: [patch(1, 'deps')] })
    const dismissed = new Set([first.id, second.id])
    expect(shouldCelebrate(first, dismissed)).toBe(false)
    expect(shouldCelebrate(second, dismissed)).toBe(false)
    expect(shouldCelebrate({ ...first, id: 'third' }, dismissed)).toBe(true)
  })

  it('tolerates a run missing patches and targetState', () => {
    const run = { id: 'x', status: 'completed' }

    expect(shouldCelebrate(run, null)).toBe(false)
  })
})

describe('summarizeHeal', () => {
  it('returns empty values for a null run', () => {
    expect(summarizeHeal(null)).toEqual({ patchCount: 0, categories: [], urls: [] })
  })

  it('counts every patch but dedupes categories in first-seen order', () => {
    // Arrange
    const run = makeRun({ patches: [patch(1, 'port'), patch(2, 'deps'), patch(3, 'port'), patch(4, null)] })

    // Act
    const { patchCount, categories } = summarizeHeal(run)

    // Assert
    expect(patchCount).toBe(4)
    expect(categories).toEqual(['port', 'deps'])
  })

  it('lists only done target urls in target order', () => {
    const run = makeRun({
      targets: ['node', 'local', 'cloudrun', 'function'],
      targetState: {
        local: target({ url: 'https://local.example' }),
        node: target({ url: 'https://node.example' }),
        cloudrun: target({ phase: 'failed', url: 'https://stale.example' }),
        function: target({ url: null }),
      },
    })

    expect(summarizeHeal(run).urls).toEqual(['https://node.example', 'https://local.example'])
  })

  it('drops urls that are not http or https', () => {
    const run = makeRun({
      targets: ['local', 'node'],
      targetState: {
        local: target({ url: 'javascript:alert(1)' }),
        node: target({ url: 'HTTP://node.example' }),
      },
    })

    expect(summarizeHeal(run).urls).toEqual(['HTTP://node.example'])
  })

  it('skips targets that have no state entry', () => {
    const run = makeRun({ targets: ['local', 'ghost'] })

    expect(summarizeHeal(run).urls).toEqual(['https://a.example'])
  })
})
