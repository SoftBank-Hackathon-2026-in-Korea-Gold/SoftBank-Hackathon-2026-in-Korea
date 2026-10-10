// Node health classification and layout helpers for the instanced fleet view. Pure functions only, so the
// 3D component stays thin and this file is testable in node.

// Mirrors the backend's NODE_LOAD_HOT: 1-min load average per core at or above this is overloaded.
export const NODE_LOAD_HOT = 0.8

export const STATUSES = ['healthy', 'overloaded', 'failed']

// mode 'breathe' is a smooth sine, 'blink' is a hard on/off square wave for failed nodes.
export const STATUS_STYLE = {
  healthy: { color: '#10b981', label: '정상', tone: 'emerald', pulse: { speed: 1.2, scale: 0.04, glow: 0.18, mode: 'breathe' } },
  overloaded: { color: '#f97316', label: '과부하', tone: 'amber', pulse: { speed: 3.6, scale: 0.1, glow: 0.35, mode: 'breathe' } },
  failed: { color: '#ef4444', label: '장애', tone: 'rose', pulse: { speed: 2.4, scale: 0, glow: 0.6, mode: 'blink' } },
}

const toNumber = (v, fallback) => {
  const n = Number(v)
  return Number.isFinite(n) ? n : fallback
}

export function loadRatio(node) {
  if (!node?.ok) return 0
  const load = Math.max(0, toNumber(node.load1, 0))
  const cpus = Math.max(1, toNumber(node.cpus, 1))
  return load / cpus
}

export function classifyNode(node) {
  if (!node?.ok) return 'failed'
  return loadRatio(node) >= NODE_LOAD_HOT ? 'overloaded' : 'healthy'
}

export function countByStatus(nodes) {
  const counts = { healthy: 0, overloaded: 0, failed: 0 }
  for (const n of nodes || []) counts[classifyNode(n)] += 1
  return counts
}

// Near-square grid on the XZ plane whose bounding box is centred on the origin.
export function gridLayout(count, spacing = 1) {
  const n = Math.max(0, Math.floor(toNumber(count, 0)))
  if (n === 0) return []
  const cols = Math.ceil(Math.sqrt(n))
  const rows = Math.ceil(n / cols)
  const x0 = ((cols - 1) * spacing) / 2
  const z0 = ((rows - 1) * spacing) / 2
  return Array.from({ length: n }, (_, i) => [(i % cols) * spacing - x0, 0, Math.floor(i / cols) * spacing - z0])
}

// drei's <Instances> allocates its buffers once from `limit`, so the component remounts it when the
// capacity bucket changes. Power-of-two buckets keep that to a handful of remounts as the fleet grows.
const MIN_CAPACITY = 16

export function instanceCapacity(count) {
  const n = Math.max(1, Math.floor(toNumber(count, 1)))
  let cap = MIN_CAPACITY
  while (cap < n) cap *= 2
  return cap
}

const CAMERA_BASE = 4
const CAMERA_PER_SQRT = 1.6
export const FLEET_CAMERA_FOV = 45
export const RACK_SIZE = [0.8, 1.2, 0.8]

// Fit a sphere around the racks using the narrower field of view. The sphere also fits while orbiting.
export function cameraDistance(count, spacing = 1, aspect = 1) {
  const n = Math.max(1, Math.floor(toNumber(count, 1)))
  const cols = Math.ceil(Math.sqrt(n))
  const rows = Math.ceil(n / cols)
  const halfWidth = ((cols - 1) * spacing + RACK_SIZE[0]) / 2
  const halfDepth = ((rows - 1) * spacing + RACK_SIZE[2]) / 2
  const radius = Math.hypot(halfWidth, halfDepth, RACK_SIZE[1] * 1.1)
  const halfV = (FLEET_CAMERA_FOV * Math.PI) / 360
  const safeAspect = toNumber(aspect, 1) > 0 ? toNumber(aspect, 1) : 1
  const halfFov = Math.min(halfV, Math.atan(Math.tan(halfV) * safeAspect))
  return Math.max(CAMERA_BASE + CAMERA_PER_SQRT * Math.sqrt(n) * spacing, radius / Math.sin(halfFov) * 1.1)
}

// Per-instance pulse at time t (seconds). Returns a vertical scale factor and a signed colour mix:
// positive brightens toward white, negative darkens toward black.
export function pulseAt(status, t, phase = 0, intensity = 1) {
  const p = (STATUS_STYLE[status] || STATUS_STYLE.healthy).pulse
  const wave = Math.sin(t * p.speed + phase)
  if (p.mode === 'blink') return { scaleY: 1, mix: wave >= 0 ? 0 : -p.glow * intensity }
  const w = (wave + 1) / 2
  return { scaleY: 1 + p.scale * intensity * w, mix: p.glow * intensity * w }
}

// Golden-angle offsets so neighbouring cubes never pulse in lockstep.
const GOLDEN_ANGLE = 2.399963229728653

export const phaseFor = (index) => (index * GOLDEN_ANGLE) % (Math.PI * 2)

// mulberry32: tiny seeded PRNG so demo fleets are identical on every render and in tests.
export function seededRandom(seed) {
  let a = Math.floor(toNumber(seed, 0)) >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const DEMO_CPUS = [2, 4, 8, 16]
const DEMO_FAILED_P = 0.05
const DEMO_OVERLOADED_P = 0.15
const DEMO_HEALTHY_MAX_RATIO = 0.7
const DEMO_HOT_MIN_RATIO = 0.85
const DEMO_HOT_SPAN = 0.75

// Synthetic nodes in the GET /fleet node shape (~80% healthy / 15% overloaded / 5% failed). Opt-in only.
export function demoNodes(count, seed = 7) {
  const n = Math.max(0, Math.floor(toNumber(count, 0)))
  const rand = seededRandom(seed)
  return Array.from({ length: n }, (_, i) => {
    const roll = rand()
    const cpus = DEMO_CPUS[Math.floor(rand() * DEMO_CPUS.length)]
    const memTotal = cpus * 2048
    const node = `demo-node-${String(i + 1).padStart(3, '0')}`
    if (roll < DEMO_FAILED_P) {
      return { node, ok: false, load1: 0, cpus, mem_total_mb: memTotal, mem_avail_mb: 0, provider: 'demo', demo: true }
    }
    const hot = roll < DEMO_FAILED_P + DEMO_OVERLOADED_P
    const ratio = hot ? DEMO_HOT_MIN_RATIO + rand() * DEMO_HOT_SPAN : rand() * DEMO_HEALTHY_MAX_RATIO
    const load1 = Math.round(ratio * cpus * 100) / 100
    const memAvail = Math.round(memTotal * (0.2 + rand() * 0.6))
    return { node, ok: true, load1, cpus, mem_total_mb: memTotal, mem_avail_mb: memAvail, provider: 'demo', demo: true }
  })
}
