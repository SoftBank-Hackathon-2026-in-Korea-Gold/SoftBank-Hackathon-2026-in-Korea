// Where the "source code" cube sits on the 3D deploy track, derived from the same run object the Stepper reads.
// Station labels mirror the Stepper; heal is a detour off the main line between deploy and live.
import { stepStatus } from './run'

export const START_PAD = { id: 'idle', label: '소스', position: [-4, 0, 0] }

export const STATIONS = [
  { id: 'analyze', label: '코드 분석', position: [-2, 0, 0] },
  { id: 'deploy', label: '빌드 & 배포', position: [0, 0, 0] },
  { id: 'heal', label: 'AI 자가치유', position: [2, 0, 1.6] },
  { id: 'live', label: '공개 URL', position: [4, 0, 0] },
]

const POS = Object.fromEntries([START_PAD, ...STATIONS].map((s) => [s.id, s.position]))

// Main line runs straight; the detour leaves at deploy and rejoins at live.
export const TRACK_SEGMENTS = [
  { from: 'idle', to: 'analyze', detour: false },
  { from: 'analyze', to: 'deploy', detour: false },
  { from: 'deploy', to: 'live', detour: false },
  { from: 'deploy', to: 'heal', detour: true },
  { from: 'heal', to: 'live', detour: true },
]

/** Furthest station the run has actually reached, ignoring 'live' (only a finished run gets there). */
function reachedStation(run, phases) {
  if (run.patches.length > 0 || phases.includes('healing')) return 'heal'
  // Cancel overwrites unfinished phases, so currentTarget (set by the deploy stage) is the reliable signal.
  if (run.currentTarget || phases.some((p) => p !== 'pending' && p !== 'cancelled')) return 'deploy'
  return 'analyze'
}

/** { station, mode, healed } for the cube. mode: idle | moving | failed | done | partial | cancelled */
export function journeyState(run) {
  if (!run || run.status === 'idle') return { station: 'idle', mode: 'idle', healed: false }
  const steps = stepStatus(run)
  const healed = run.patches.length > 0
  const phases = Object.values(run.targetState).map((s) => s.phase)
  const reached = reachedStation(run, phases)

  if (run.status === 'cancelled') return { station: phases.includes('done') ? 'live' : reached, mode: 'cancelled', healed }
  if (run.status === 'completed') return { station: 'live', mode: 'done', healed }
  if (run.status === 'partial_failure') return { station: 'live', mode: 'partial', healed }
  if (run.status === 'failed') {
    // Analysis finished but nothing deployed (e.g. env input timed out): the Stepper marks deploy as failed.
    const station = reached === 'analyze' && steps.analyze === 'done' ? 'deploy' : reached
    return { station, mode: 'failed', healed }
  }

  // Heal wins over deploy when several targets run at once; it also stays active through redeploy.
  if (steps.heal === 'active') return { station: 'heal', mode: 'moving', healed }
  if (steps.deploy === 'active') return { station: 'deploy', mode: 'moving', healed }
  if (steps.analyze === 'active') return { station: 'analyze', mode: 'moving', healed }
  // Running with no active step: waiting on env input / queue, or between targets.
  return { station: reached, mode: 'moving', healed }
}

/** Ordered waypoints from the start pad to `station`, through the heal detour when the run was healed. */
export function pathTo(station, healed = false) {
  const route = {
    idle: ['idle'],
    analyze: ['idle', 'analyze'],
    deploy: ['idle', 'analyze', 'deploy'],
    heal: ['idle', 'analyze', 'deploy', 'heal'],
    live: healed ? ['idle', 'analyze', 'deploy', 'heal', 'live'] : ['idle', 'analyze', 'deploy', 'live'],
  }[station] || ['idle']
  return route.map((id) => POS[id])
}

/** Move on to the next waypoint once the cube is within `arriveDist` of the current one. */
export function advanceWaypoint(index, distance, lastIndex, arriveDist = 0.08) {
  return distance < arriveDist && index < lastIndex ? index + 1 : Math.min(index, lastIndex)
}

const samePoint = (a, b) => a[0] === b[0] && a[1] === b[1] && a[2] === b[2]

/** Waypoint index to continue from when the path changes: stay on the shared prefix, never past it. */
export function resumeIndex(prevPath, nextPath, prevIndex) {
  let shared = 0
  while (shared < prevPath.length && shared < nextPath.length && samePoint(prevPath[shared], nextPath[shared])) shared++
  return Math.max(0, Math.min(prevIndex, shared - 1, nextPath.length - 1))
}

/** Center, length and Y rotation of a flat box spanning two points on the ground plane. */
export function segmentTransform(from, to) {
  const dx = to[0] - from[0]
  const dz = to[2] - from[2]
  return {
    center: [(from[0] + to[0]) / 2, (from[1] + to[1]) / 2, (from[2] + to[2]) / 2],
    length: Math.hypot(dx, dz),
    // three.js +Y rotation turns +X toward -Z, hence the negated dz.
    rotationY: Math.atan2(-dz, dx),
  }
}

export const segmentPoints = (seg) => [POS[seg.from], POS[seg.to]]

/** Camera distance that keeps `halfWidth` world units visible horizontally at the given aspect. */
export function cameraDistance(aspect, { halfWidth = 5.2, vfovDeg = 30, minDistance = 9 } = {}) {
  const safeAspect = aspect > 0 ? aspect : 1
  const halfV = (vfovDeg * Math.PI) / 360
  const halfH = Math.atan(Math.tan(halfV) * safeAspect)
  return Math.max(minDistance, halfWidth / Math.tan(halfH))
}
