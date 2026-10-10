import { describe, it, expect } from 'vitest'
import {
  NODE_LOAD_HOT,
  STATUS_STYLE,
  cameraDistance,
  classifyNode,
  countByStatus,
  demoNodes,
  gridLayout,
  instanceCapacity,
  loadRatio,
  phaseFor,
  pulseAt,
  seededRandom,
} from './fleetStatus'

describe('classifyNode', () => {
  it('marks an unreachable node as failed regardless of load', () => {
    // Arrange
    const node = { node: 'a', ok: false, load1: 0.1, cpus: 4 }
    // Act
    const status = classifyNode(node)
    // Assert
    expect(status).toBe('failed')
  })

  it('treats null or undefined nodes as failed', () => {
    expect(classifyNode(null)).toBe('failed')
    expect(classifyNode(undefined)).toBe('failed')
  })

  it('marks a node overloaded at exactly the hot threshold', () => {
    const node = { ok: true, load1: NODE_LOAD_HOT * 4, cpus: 4 }
    expect(classifyNode(node)).toBe('overloaded')
  })

  it('keeps a node healthy just below the hot threshold', () => {
    const node = { ok: true, load1: 3.19, cpus: 4 }
    expect(classifyNode(node)).toBe('healthy')
  })

  it('treats cpus of 0 as a single core instead of dividing by zero', () => {
    const node = { ok: true, load1: 0.8, cpus: 0 }
    expect(loadRatio(node)).toBe(0.8)
    expect(classifyNode(node)).toBe('overloaded')
  })

  it('treats a missing cpus field as a single core', () => {
    const node = { ok: true, load1: 0.5 }
    expect(loadRatio(node)).toBe(0.5)
    expect(classifyNode(node)).toBe('healthy')
  })

  it('treats NaN or missing load as idle', () => {
    expect(classifyNode({ ok: true, load1: Number.NaN, cpus: 2 })).toBe('healthy')
    expect(classifyNode({ ok: true, cpus: 2 })).toBe('healthy')
    expect(loadRatio({ ok: true, load1: 'oops', cpus: 2 })).toBe(0)
  })

  it('accepts numeric strings from loosely typed payloads', () => {
    expect(classifyNode({ ok: true, load1: '3.6', cpus: '4' })).toBe('overloaded')
  })
})

describe('STATUS_STYLE', () => {
  it('defines colour, Korean label and pulse params for every status', () => {
    expect(STATUS_STYLE.healthy).toMatchObject({ color: '#10b981', label: '정상' })
    expect(STATUS_STYLE.overloaded).toMatchObject({ color: '#f97316', label: '과부하' })
    expect(STATUS_STYLE.failed).toMatchObject({ color: '#ef4444', label: '장애' })
    for (const s of Object.values(STATUS_STYLE)) expect(s.pulse.speed).toBeGreaterThan(0)
  })

  it('pulses overloaded nodes faster than healthy ones', () => {
    expect(STATUS_STYLE.overloaded.pulse.speed).toBeGreaterThan(STATUS_STYLE.healthy.pulse.speed)
  })
})

describe('countByStatus', () => {
  it('tallies nodes per status and tolerates null input', () => {
    // Arrange
    const nodes = [{ ok: true, load1: 0, cpus: 1 }, { ok: true, load1: 2, cpus: 2 }, { ok: false }]
    // Act / Assert
    expect(countByStatus(nodes)).toEqual({ healthy: 1, overloaded: 1, failed: 1 })
    expect(countByStatus(null)).toEqual({ healthy: 0, overloaded: 0, failed: 0 })
  })
})

describe('gridLayout', () => {
  it('returns one position per node', () => {
    expect(gridLayout(37, 1.5)).toHaveLength(37)
  })

  it('returns an empty array for zero, negative or invalid counts', () => {
    expect(gridLayout(0)).toEqual([])
    expect(gridLayout(-3)).toEqual([])
    expect(gridLayout(Number.NaN)).toEqual([])
  })

  it('centres the bounding box on the origin', () => {
    // Arrange / Act
    const pts = gridLayout(10, 2)
    const xs = pts.map((p) => p[0])
    const zs = pts.map((p) => p[2])
    // Assert
    expect(Math.min(...xs) + Math.max(...xs)).toBeCloseTo(0)
    expect(Math.min(...zs) + Math.max(...zs)).toBeCloseTo(0)
    expect(pts.every((p) => p[1] === 0)).toBe(true)
  })

  it('places a single node at the origin', () => {
    expect(gridLayout(1, 3)).toEqual([[0, 0, 0]])
  })

  it('lays out a near-square grid with the requested spacing', () => {
    const pts = gridLayout(9, 2)
    const xs = new Set(pts.map((p) => p[0]))
    const zs = new Set(pts.map((p) => p[2]))
    expect([...xs].sort((a, b) => a - b)).toEqual([-2, 0, 2])
    expect(zs.size).toBe(3)
  })

  it('never produces duplicate positions', () => {
    const keys = gridLayout(50, 1).map((p) => p.join(','))
    expect(new Set(keys).size).toBe(50)
  })
})

describe('instanceCapacity', () => {
  it('rounds up to a power-of-two bucket with a floor of 16', () => {
    expect(instanceCapacity(0)).toBe(16)
    expect(instanceCapacity(16)).toBe(16)
    expect(instanceCapacity(17)).toBe(32)
    expect(instanceCapacity(300)).toBe(512)
  })
})

describe('cameraDistance', () => {
  it('grows with the square root of the node count', () => {
    // Arrange / Act
    const [d25, d100, d400] = [25, 100, 400].map((n) => cameraDistance(n))
    // Assert: sqrt steps 5 -> 10 -> 20, so the second gap is twice the first
    expect(d100).toBeGreaterThan(d25)
    expect(d400 - d100).toBeCloseTo(2 * (d100 - d25))
  })

  it('clamps empty or invalid counts to a single-node framing', () => {
    expect(cameraDistance(0)).toBe(cameraDistance(1))
    expect(cameraDistance(Number.NaN)).toBe(cameraDistance(1))
  })
})

describe('pulseAt', () => {
  it('keeps healthy and overloaded pulses non-negative and bounded', () => {
    for (let t = 0; t < 10; t += 0.37) {
      const h = pulseAt('healthy', t)
      expect(h.scaleY).toBeGreaterThanOrEqual(1)
      expect(h.scaleY).toBeLessThanOrEqual(1 + STATUS_STYLE.healthy.pulse.scale + 1e-9)
      expect(h.mix).toBeGreaterThanOrEqual(0)
    }
  })

  it('blinks failed nodes between full colour and darkened without scaling', () => {
    const on = pulseAt('failed', Math.PI / 2 / STATUS_STYLE.failed.pulse.speed)
    const off = pulseAt('failed', (3 * Math.PI) / 2 / STATUS_STYLE.failed.pulse.speed)
    expect(on).toEqual({ scaleY: 1, mix: 0 })
    expect(off.scaleY).toBe(1)
    expect(off.mix).toBeLessThan(0)
  })

  it('scales the motion down with intensity for reduced motion', () => {
    const t = Math.PI / 2 / STATUS_STYLE.overloaded.pulse.speed
    const full = pulseAt('overloaded', t, 0, 1)
    const calm = pulseAt('overloaded', t, 0, 0.25)
    expect(calm.mix).toBeCloseTo(full.mix * 0.25)
  })

  it('falls back to the healthy pulse for unknown statuses', () => {
    expect(pulseAt('weird', 1)).toEqual(pulseAt('healthy', 1))
  })
})

describe('phaseFor', () => {
  it('gives neighbouring indices distinct phases within one turn', () => {
    const phases = [0, 1, 2, 3].map(phaseFor)
    expect(new Set(phases).size).toBe(4)
    expect(phases.every((p) => p >= 0 && p < Math.PI * 2)).toBe(true)
  })
})

describe('seededRandom', () => {
  it('produces the same sequence for the same seed', () => {
    const a = seededRandom(42)
    const b = seededRandom(42)
    expect([a(), a(), a()]).toEqual([b(), b(), b()])
  })

  it('stays within [0, 1)', () => {
    const r = seededRandom(1)
    for (let i = 0; i < 1000; i++) {
      const v = r()
      expect(v).toBeGreaterThanOrEqual(0)
      expect(v).toBeLessThan(1)
    }
  })
})

describe('demoNodes', () => {
  it('is deterministic for a given count and seed', () => {
    expect(demoNodes(30, 5)).toEqual(demoNodes(30, 5))
  })

  it('varies with the seed', () => {
    expect(demoNodes(30, 5)).not.toEqual(demoNodes(30, 6))
  })

  it('names nodes with zero-padded indices in the fleet node shape', () => {
    // Arrange / Act
    const nodes = demoNodes(3)
    // Assert
    expect(nodes.map((n) => n.node)).toEqual(['demo-node-001', 'demo-node-002', 'demo-node-003'])
    for (const n of nodes) {
      expect(typeof n.ok).toBe('boolean')
      expect(Number.isFinite(n.load1)).toBe(true)
      expect(n.cpus).toBeGreaterThan(0)
      expect(n.mem_total_mb).toBeGreaterThan(0)
    }
  })

  it('returns an empty list for non-positive counts', () => {
    expect(demoNodes(0)).toEqual([])
    expect(demoNodes(-5)).toEqual([])
  })

  it('mixes roughly 80% healthy, 15% overloaded and 5% failed', () => {
    // Arrange / Act
    const counts = countByStatus(demoNodes(1000, 11))
    // Assert
    expect(counts.healthy / 1000).toBeGreaterThan(0.72)
    expect(counts.healthy / 1000).toBeLessThan(0.88)
    expect(counts.overloaded / 1000).toBeGreaterThan(0.1)
    expect(counts.overloaded / 1000).toBeLessThan(0.2)
    expect(counts.failed / 1000).toBeGreaterThan(0.02)
    expect(counts.failed / 1000).toBeLessThan(0.08)
  })
})
