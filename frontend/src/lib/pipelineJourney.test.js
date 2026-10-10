import { describe, it, expect } from 'vitest'
import { applyEvent, emptyRun } from './run'
import {
  START_PAD,
  STATIONS,
  TRACK_SEGMENTS,
  advanceWaypoint,
  cameraDistance,
  journeyState,
  pathTo,
  resumeIndex,
  segmentPoints,
  segmentTransform,
} from './pipelineJourney'

const pos = (id) => (id === 'idle' ? START_PAD : STATIONS.find((s) => s.id === id)).position

// Replays [type, payload, stage] tuples through applyEvent with unique timestamps, like the SSE stream would.
function replay(events, targets = ['local']) {
  let ts = 1000
  return events.reduce((run, [type, payload, stage]) => applyEvent(run, type, payload, { stage, ts: ++ts }), emptyRun('r1', { targets }))
}

const ANALYZED = ['log', { line: 'analyzer: target=local language=python framework=fastapi port=8000' }, 'analyze']
const ANALYZE_STAGE = ['stage', {}, 'analyze']
const deploy = (target = 'local') => ['stage', { target }, 'deploy']
const heal = (target = 'local') => ['stage', { target, stderr: '[deployer] build failed' }, 'heal']
const healDiff = (attempt = 1) => ['heal_diff', { attempt, category: 'dependency', source: 'rule', rationale: 'pin version' }, 'heal']
const redeploy = (attempt = 1) => ['stage', { attempt }, 'redeploy']
const done = (target = 'local') => ['done', { target, url: `https://${target}.example` }, 'done']
const targetError = (target = 'local') => ['error', { target, message: 'boom' }, 'failed']
const runError = ['error', { message: 'env timeout' }, 'failed']
const cancel = ['error', { cancelled: true }, 'cancel']

describe('STATIONS', () => {
  it('lists the four stations in pipeline order with Stepper labels', () => {
    // Arrange
    const expected = [['analyze', '코드 분석'], ['deploy', '빌드 & 배포'], ['heal', 'AI 자가치유'], ['live', '공개 URL']]

    // Act
    const actual = STATIONS.map((s) => [s.id, s.label])

    // Assert
    expect(actual).toEqual(expected)
  })

  it('places heal off the main track between deploy and live', () => {
    // Arrange
    const [deployPos, healPos, livePos] = [pos('deploy'), pos('heal'), pos('live')]

    // Act
    const offTrack = healPos[2] !== deployPos[2] && deployPos[2] === livePos[2]
    const between = healPos[0] > deployPos[0] && healPos[0] < livePos[0]

    // Assert
    expect(offTrack).toBe(true)
    expect(between).toBe(true)
  })
})

describe('journeyState', () => {
  it('parks the cube on the start pad for a null run', () => {
    // Arrange / Act
    const state = journeyState(null)

    // Assert
    expect(state).toEqual({ station: 'idle', mode: 'idle', healed: false })
  })

  it('parks the cube on the start pad for an idle run', () => {
    // Arrange
    const run = emptyRun()

    // Act
    const state = journeyState(run)

    // Assert
    expect(state).toEqual({ station: 'idle', mode: 'idle', healed: false })
  })

  it('moves to analyze while the analyzer is reading the repo', () => {
    // Arrange
    const run = replay([ANALYZE_STAGE])

    // Act
    const state = journeyState(run)

    // Assert
    expect(state).toEqual({ station: 'analyze', mode: 'moving', healed: false })
  })

  it('waits at analyze when analysis is done but no target has started yet', () => {
    // Arrange
    const run = replay([ANALYZE_STAGE, ANALYZED])

    // Act
    const state = journeyState(run)

    // Assert
    expect(state).toEqual({ station: 'analyze', mode: 'moving', healed: false })
  })

  it('moves to deploy while a target is deploying', () => {
    // Arrange
    const run = replay([ANALYZED, deploy()])

    // Act
    const state = journeyState(run)

    // Assert
    expect(state).toEqual({ station: 'deploy', mode: 'moving', healed: false })
  })

  it('takes the heal detour as soon as a target starts healing, before any patch exists', () => {
    // Arrange
    const run = replay([ANALYZED, deploy(), heal()])

    // Act
    const state = journeyState(run)

    // Assert
    expect(state).toEqual({ station: 'heal', mode: 'moving', healed: false })
  })

  it('marks the run healed once a patch arrives', () => {
    // Arrange
    const run = replay([ANALYZED, deploy(), heal(), healDiff()])

    // Act
    const state = journeyState(run)

    // Assert
    expect(state).toEqual({ station: 'heal', mode: 'moving', healed: true })
  })

  it('stays at heal during redeploy because the phase remains healing', () => {
    // Arrange
    const run = replay([ANALYZED, deploy(), heal(), healDiff(), redeploy()])

    // Act
    const state = journeyState(run)

    // Assert
    expect(state).toEqual({ station: 'heal', mode: 'moving', healed: true })
  })

  it('prefers heal over deploy when targets are in both phases', () => {
    // Arrange
    const run = replay([ANALYZED, deploy('local'), heal('local'), deploy('cloudrun')], ['local', 'cloudrun'])

    // Act
    const state = journeyState(run)

    // Assert
    expect(state.station).toBe('heal')
    expect(state.mode).toBe('moving')
  })

  it('reaches live with done after a clean completion', () => {
    // Arrange
    const run = replay([ANALYZED, deploy(), done()])

    // Act
    const state = journeyState(run)

    // Assert
    expect(run.status).toBe('completed')
    expect(state).toEqual({ station: 'live', mode: 'done', healed: false })
  })

  it('reaches live through the detour after a healed completion', () => {
    // Arrange
    const run = replay([ANALYZED, deploy(), heal(), healDiff(), redeploy(), done()])

    // Act
    const state = journeyState(run)

    // Assert
    expect(state).toEqual({ station: 'live', mode: 'done', healed: true })
  })

  it('reaches live with partial when one of two targets fails', () => {
    // Arrange
    const run = replay([ANALYZED, deploy('local'), done('local'), deploy('cloudrun'), targetError('cloudrun')], ['local', 'cloudrun'])

    // Act
    const state = journeyState(run)

    // Assert
    expect(run.status).toBe('partial_failure')
    expect(state).toEqual({ station: 'live', mode: 'partial', healed: false })
  })

  it('does not reach live while a second target is still pending', () => {
    // Arrange
    const run = replay([ANALYZED, deploy('local'), done('local')], ['local', 'cloudrun'])

    // Act
    const state = journeyState(run)

    // Assert
    expect(run.status).toBe('running')
    expect(state).toEqual({ station: 'deploy', mode: 'moving', healed: false })
  })

  it('fails at analyze when the run dies before analysis completes', () => {
    // Arrange
    const run = replay([ANALYZE_STAGE, runError])

    // Act
    const state = journeyState(run)

    // Assert
    expect(state).toEqual({ station: 'analyze', mode: 'failed', healed: false })
  })

  it('fails at deploy when analysis finished but no target ever started', () => {
    // Arrange
    const run = replay([ANALYZED, runError])

    // Act
    const state = journeyState(run)

    // Assert
    expect(state).toEqual({ station: 'deploy', mode: 'failed', healed: false })
  })

  it('fails at deploy when the only target fails without healing', () => {
    // Arrange
    const run = replay([ANALYZED, deploy(), targetError()])

    // Act
    const state = journeyState(run)

    // Assert
    expect(run.status).toBe('failed')
    expect(state).toEqual({ station: 'deploy', mode: 'failed', healed: false })
  })

  it('fails at heal when the target fails after patches were tried', () => {
    // Arrange
    const run = replay([ANALYZED, deploy(), heal(), healDiff(), redeploy(), targetError()])

    // Act
    const state = journeyState(run)

    // Assert
    expect(state).toEqual({ station: 'heal', mode: 'failed', healed: true })
  })

  it('stops at analyze when cancelled during analysis even though targets were pre-seeded', () => {
    // Arrange
    const run = replay([ANALYZE_STAGE, cancel])

    // Act
    const state = journeyState(run)

    // Assert
    expect(run.targetState.local.phase).toBe('cancelled')
    expect(state).toEqual({ station: 'analyze', mode: 'cancelled', healed: false })
  })

  it('stops at deploy when cancelled mid-deploy', () => {
    // Arrange
    const run = replay([ANALYZED, deploy(), cancel])

    // Act
    const state = journeyState(run)

    // Assert
    expect(state).toEqual({ station: 'deploy', mode: 'cancelled', healed: false })
  })

  it('stops at heal when cancelled after a patch', () => {
    // Arrange
    const run = replay([ANALYZED, deploy(), heal(), healDiff(), cancel])

    // Act
    const state = journeyState(run)

    // Assert
    expect(state).toEqual({ station: 'heal', mode: 'cancelled', healed: true })
  })

  it('stops at live when cancelled after one target already went live', () => {
    // Arrange
    const run = replay([ANALYZED, deploy('local'), done('local'), deploy('cloudrun'), cancel], ['local', 'cloudrun'])

    // Act
    const state = journeyState(run)

    // Assert
    expect(state).toEqual({ station: 'live', mode: 'cancelled', healed: false })
  })
})

describe('pathTo', () => {
  it('returns only the start pad for idle', () => {
    // Arrange / Act
    const path = pathTo('idle')

    // Assert
    expect(path).toEqual([START_PAD.position])
  })

  it('falls back to the start pad for an unknown station', () => {
    // Arrange / Act
    const path = pathTo('nowhere')

    // Assert
    expect(path).toEqual([START_PAD.position])
  })

  it('walks the main track up to deploy', () => {
    // Arrange / Act
    const path = pathTo('deploy')

    // Assert
    expect(path).toEqual([pos('idle'), pos('analyze'), pos('deploy')])
  })

  it('always passes through heal when the heal station is the goal', () => {
    // Arrange / Act
    const path = pathTo('heal', false)

    // Assert
    expect(path).toEqual([pos('idle'), pos('analyze'), pos('deploy'), pos('heal')])
  })

  it('goes straight from deploy to live when the run was not healed', () => {
    // Arrange / Act
    const path = pathTo('live', false)

    // Assert
    expect(path).toEqual([pos('idle'), pos('analyze'), pos('deploy'), pos('live')])
  })

  it('takes the heal detour to live when the run was healed', () => {
    // Arrange / Act
    const path = pathTo('live', true)

    // Assert
    expect(path).toEqual([pos('idle'), pos('analyze'), pos('deploy'), pos('heal'), pos('live')])
  })
})

describe('advanceWaypoint', () => {
  it('advances when the cube has arrived and more waypoints remain', () => {
    // Arrange / Act
    const next = advanceWaypoint(1, 0.01, 3)

    // Assert
    expect(next).toBe(2)
  })

  it('holds while the cube is still travelling', () => {
    // Arrange / Act
    const next = advanceWaypoint(1, 0.5, 3)

    // Assert
    expect(next).toBe(1)
  })

  it('never advances past the last waypoint', () => {
    // Arrange / Act
    const next = advanceWaypoint(3, 0, 3)

    // Assert
    expect(next).toBe(3)
  })

  it('clamps an index that is beyond a shorter path', () => {
    // Arrange / Act
    const next = advanceWaypoint(4, 1, 2)

    // Assert
    expect(next).toBe(2)
  })
})

describe('resumeIndex', () => {
  it('keeps the current index when the path is extended', () => {
    // Arrange
    const prev = pathTo('deploy')
    const next = pathTo('heal')

    // Act
    const index = resumeIndex(prev, next, 2)

    // Assert
    expect(index).toBe(2)
  })

  it('backs up to deploy when a second target pulls the cube off the heal detour', () => {
    // Arrange
    const prev = pathTo('heal', true)
    const next = pathTo('deploy', true)

    // Act
    const index = resumeIndex(prev, next, 3)

    // Assert
    expect(index).toBe(2)
  })

  it('rewinds to the start pad when a new run resets the journey', () => {
    // Arrange
    const prev = pathTo('live', true)
    const next = pathTo('idle')

    // Act
    const index = resumeIndex(prev, next, 4)

    // Assert
    expect(index).toBe(0)
  })

  it('stops at the fork when the route after deploy changes', () => {
    // Arrange
    const prev = pathTo('live', false)
    const next = pathTo('live', true)

    // Act
    const index = resumeIndex(prev, next, 3)

    // Assert
    expect(index).toBe(2)
  })
})

describe('segmentTransform', () => {
  it('spans a straight segment along +X with no rotation', () => {
    // Arrange / Act
    const t = segmentTransform([0, 0, 0], [4, 0, 0])

    // Assert
    expect(t.center).toEqual([2, 0, 0])
    expect(t.length).toBe(4)
    expect(t.rotationY).toBeCloseTo(0)
  })

  it('rotates a segment heading toward +Z by -90 degrees', () => {
    // Arrange / Act
    const t = segmentTransform([0, 0, 0], [0, 0, 2])

    // Assert
    expect(t.length).toBe(2)
    expect(t.rotationY).toBeCloseTo(-Math.PI / 2)
  })
})

describe('TRACK_SEGMENTS', () => {
  it('connects known stations and marks only the heal legs as detour', () => {
    // Arrange / Act
    const points = TRACK_SEGMENTS.map(segmentPoints)
    const detours = TRACK_SEGMENTS.filter((s) => s.detour).map((s) => `${s.from}-${s.to}`)

    // Assert
    expect(points.every(([a, b]) => Array.isArray(a) && Array.isArray(b))).toBe(true)
    expect(detours).toEqual(['deploy-heal', 'heal-live'])
  })
})

describe('cameraDistance', () => {
  it('pulls the camera back on narrow viewports', () => {
    // Arrange / Act
    const wide = cameraDistance(4)
    const narrow = cameraDistance(1.2)

    // Assert
    expect(narrow).toBeGreaterThan(wide)
  })

  it('never goes closer than the minimum distance', () => {
    // Arrange / Act
    const d = cameraDistance(10, { minDistance: 9 })

    // Assert
    expect(d).toBe(9)
  })

  it('treats an invalid aspect as square', () => {
    // Arrange / Act
    const d = cameraDistance(0)

    // Assert
    expect(d).toBeCloseTo(cameraDistance(1))
  })
})
