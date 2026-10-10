import { describe, expect, it } from 'vitest'
import { emptyRun } from './run'
import { activeStepLabel, liveEndpoints, pendingInput, recentKeyLogs } from './dashboard'

describe('dashboard summaries', () => {
  it('keeps required input visible and prioritizes environment values', () => {
    const run = { ...emptyRun('a'), envRequest: { items: [] }, question: { ask: true, question_id: 'q' } }
    expect(pendingInput(run)).toEqual({ key: 'a:env', label: '배포에 필요한 값 입력' })
    expect(activeStepLabel(run)).toBe('입력 대기')
    run.envRequest = null
    expect(pendingInput(run).key).toBe('a:question:q')
    run.question.choice = 'continue'
    expect(pendingInput(run)).toBeNull()
  })

  it('keeps the latest key events in time order without mutating the complete log', () => {
    const logs = ['stage', 'info', 'heal', 'note', 'error', 'success'].map((kind, id) => ({ kind, id }))
    expect(recentKeyLogs(logs).map((l) => l.id)).toEqual([2, 4, 5])
    expect(logs).toHaveLength(6)
  })

  it('offers only successful web endpoints that have not been stopped', () => {
    const run = { ...emptyRun('a', { targets: ['local', 'cloudrun', 'node'] }), targetState: {
      local: { phase: 'done', url: 'https://local.example' },
      cloudrun: { phase: 'done', url: 'javascript:alert(1)' },
      node: { phase: 'failed', url: 'https://stale.example' },
    } }
    expect(liveEndpoints(run).map((s) => s.target)).toEqual(['local'])
    expect(liveEndpoints(run, { last_status: 'stopped', live: {} })).toEqual([])
  })
})
