import React, { Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import * as THREE from 'three';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import { Html, RoundedBox } from '@react-three/drei';
import { stepStatus } from '../../lib/run';
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
} from '../../lib/pipelineJourney';

const CAMERA_FOV = 30;
const CAMERA_ELEVATION = THREE.MathUtils.degToRad(34);
const LOOK_AT_Z = 0.7;
const MAX_DELTA = 1 / 20; // clamp so a backgrounded tab doesn't teleport the cube on return

const CUBE_SIZE = 0.55;
const CUBE_Y = 0.42;
const MOVE_LAMBDA = 3.2;
const COLOR_LAMBDA = 4;
const ARRIVE_DIST = 0.08;
const BOB_AMP = 0.07;
const BOB_SPEED = 4.5;
const SPIN_SPEED = 0.9;
const TILT = 0.12;
const SHAKE_AMP = 0.07;
const SHAKE_FREQ = 42;
const SHAKE_SECONDS = 0.9;
const PULSE_SPEED = 5;
const Y_LAMBDA = 12;
const TILT_LAMBDA = 4;
const GLOW_LAMBDA = 6;
const TILT_FREQ_X = 1.7;
const TILT_FREQ_Z = 1.3;
const HEAL_PULSE = 0.45;
const GLOW = { idle: 0.2, moving: 0.7, done: 0.85, partial: 0.6, failed: 0.75, cancelled: 0.12 };

const PAD_RADIUS = 0.48;
const LABEL_OFFSET = [0, 0.02, 0.88];
const HTML_Z = [10, 0]; // keep labels under app overlays (modals use z-50)
const TRACK_WIDTH = 0.62;
const STRIPE_GAP = 0.32;
const STRIPE_SPEED = 0.9;
const PAD_GLOW = 0.45;
const PAD_GLOW_IDLE = 0.05;
const PAD_PULSE = 0.25;
const RING_SPEED = 0.8;
const RING_GROWTH = 0.8;
const RING_OPACITY = 0.55;

const TONE = {
  violet: new THREE.Color('#8b5cf6'),
  amber: new THREE.Color('#f59e0b'),
  emerald: new THREE.Color('#10b981'),
  rose: new THREE.Color('#f43f5e'),
  slate: new THREE.Color('#64748b'),
  dim: new THREE.Color('#334155'),
  faint: new THREE.Color('#1e293b'),
};

const PAD_TONE = { idle: 'dim', active: 'violet', done: 'emerald', failed: 'rose', partial: 'amber', skipped: 'faint', cancelled: 'slate' };

const LABEL_CLASS = {
  idle: 'border-white/10 bg-slate-950/70 text-slate-500',
  active: 'border-violet-500/50 bg-violet-500/15 text-violet-100',
  done: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200',
  failed: 'border-rose-500/50 bg-rose-500/15 text-rose-100',
  partial: 'border-amber-500/50 bg-amber-500/15 text-amber-100',
  skipped: 'border-white/5 bg-slate-950/70 text-slate-600',
  cancelled: 'border-white/10 bg-slate-950/70 text-slate-500',
};

const MODE_LABEL = { idle: '대기 중', moving: '진행 중', done: '배포 완료', partial: '일부 실패', failed: '실패', cancelled: '중단됨' };
const MODE_BADGE = {
  idle: 'text-slate-500',
  moving: 'text-violet-300',
  done: 'text-emerald-300',
  partial: 'text-amber-300',
  failed: 'text-rose-300',
  cancelled: 'text-slate-400',
};

const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';
const CAMERA = { fov: CAMERA_FOV, position: [0, 6, 10] };
const GL = { antialias: true, alpha: true };

const STATION_LABEL = Object.fromEntries([START_PAD, ...STATIONS].map((s) => [s.id, s.label]));

function cubeTone(mode, station) {
  if (mode === 'moving') return station === 'heal' ? 'amber' : 'violet';
  return { idle: 'slate', done: 'emerald', partial: 'amber', failed: 'rose', cancelled: 'slate' }[mode] || 'violet';
}

function useReducedMotion() {
  const [reduced, setReduced] = useState(() => typeof window !== 'undefined' && !!window.matchMedia?.(REDUCED_MOTION_QUERY).matches);
  useEffect(() => {
    const mql = window.matchMedia?.(REDUCED_MOTION_QUERY);
    if (!mql) return undefined;
    const onChange = (e) => setReduced(e.matches);
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, []);
  return reduced;
}

function CameraRig() {
  const { camera, size } = useThree();
  useEffect(() => {
    const d = cameraDistance(size.width / Math.max(1, size.height), { vfovDeg: CAMERA_FOV });
    camera.position.set(0, d * Math.sin(CAMERA_ELEVATION), LOOK_AT_Z + d * Math.cos(CAMERA_ELEVATION));
    camera.lookAt(0, 0, LOOK_AT_Z);
    camera.updateProjectionMatrix();
  }, [camera, size.width, size.height]);
  return null;
}

function TrackSegment({ segment, lit, running, reduced }) {
  const { center, length, rotationY } = useMemo(() => segmentTransform(...segmentPoints(segment)), [segment]);
  const count = Math.max(2, Math.round(length / STRIPE_GAP));
  const stripes = useRef(null);
  const offset = useRef(0);
  const dummy = useMemo(() => new THREE.Object3D(), []);

  useFrame((_, delta) => {
    const mesh = stripes.current;
    if (!mesh) return;
    if (running && !reduced) offset.current = (offset.current + Math.min(delta, MAX_DELTA) * STRIPE_SPEED) % length;
    for (let i = 0; i < count; i++) {
      const x = ((i * (length / count) + offset.current) % length) - length / 2;
      dummy.position.set(x, 0.045, 0);
      dummy.updateMatrix();
      mesh.setMatrixAt(i, dummy.matrix);
    }
    mesh.instanceMatrix.needsUpdate = true;
  });

  const accent = segment.detour ? '#f59e0b' : '#8b5cf6';
  return (
    <group position={center} rotation-y={rotationY}>
      <mesh receiveShadow>
        <boxGeometry args={[length, 0.06, TRACK_WIDTH]} />
        <meshStandardMaterial color={lit ? '#1e1b3a' : '#111827'} roughness={0.85} />
      </mesh>
      {[-1, 1].map((side) => (
        <mesh key={side} position={[0, 0.04, (side * TRACK_WIDTH) / 2]}>
          <boxGeometry args={[length, 0.06, 0.04]} />
          <meshStandardMaterial color={lit ? accent : '#334155'} emissive={lit ? accent : '#000000'} emissiveIntensity={lit ? 0.35 : 0} />
        </mesh>
      ))}
      {/* Matrices move every frame, so the stale bounding sphere must not cull them. */}
      <instancedMesh ref={stripes} args={[undefined, undefined, count]} frustumCulled={false}>
        <boxGeometry args={[0.05, 0.015, TRACK_WIDTH * 0.8]} />
        <meshStandardMaterial color={lit ? accent : '#475569'} emissive={lit ? accent : '#000000'} emissiveIntensity={lit ? 0.6 : 0} />
      </instancedMesh>
    </group>
  );
}

function StationPad({ position, label, state, celebrate, reduced, portal }) {
  const material = useRef(null);
  const ring = useRef(null);
  const tone = TONE[PAD_TONE[state] || 'dim'];

  useFrame(({ clock }) => {
    const t = clock.elapsedTime;
    if (material.current) {
      const pulse = state === 'active' && !reduced ? PAD_PULSE * (1 + Math.sin(t * PULSE_SPEED)) : 0;
      material.current.emissiveIntensity = (state === 'idle' || state === 'skipped' ? PAD_GLOW_IDLE : PAD_GLOW) + pulse;
    }
    if (ring.current) {
      const phase = reduced ? 0.5 : (t * RING_SPEED) % 1;
      ring.current.scale.setScalar(1 + phase * RING_GROWTH);
      ring.current.material.opacity = RING_OPACITY * (1 - phase);
    }
  });

  return (
    <group position={position}>
      <mesh position={[0, 0.06, 0]}>
        <cylinderGeometry args={[PAD_RADIUS, PAD_RADIUS * 1.08, 0.12, 40]} />
        <meshStandardMaterial ref={material} color={tone} emissive={tone} roughness={0.5} metalness={0.2} />
      </mesh>
      {celebrate && (
        <mesh ref={ring} position={[0, 0.13, 0]} rotation-x={-Math.PI / 2}>
          <ringGeometry args={[PAD_RADIUS * 1.05, PAD_RADIUS * 1.2, 48]} />
          <meshBasicMaterial color={TONE.emerald} transparent depthWrite={false} />
        </mesh>
      )}
      <Html position={LABEL_OFFSET} center zIndexRange={HTML_Z} pointerEvents="none" portal={portal}>
        <span className={`pointer-events-none select-none whitespace-nowrap rounded-md border px-1.5 py-0.5 text-[10px] font-semibold ${LABEL_CLASS[state] || LABEL_CLASS.idle}`}>
          {label}
        </span>
      </Html>
    </group>
  );
}

function CodeCube({ path, mode, station, reduced, portal }) {
  const group = useRef(null);
  const body = useRef(null);
  const material = useRef(null);
  const pathRef = useRef(path);
  const index = useRef(0);
  const prevMode = useRef(mode);
  const shakeStart = useRef(-Infinity);

  useLayoutEffect(() => {
    const [x, , z] = START_PAD.position;
    group.current?.position.set(x, CUBE_Y, z);
  }, []);

  useFrame(({ clock }, rawDelta) => {
    const g = group.current;
    const m = material.current;
    if (!g || !body.current || !m) return;
    const delta = Math.min(rawDelta, MAX_DELTA);
    const t = clock.elapsedTime;

    if (pathRef.current !== path) {
      index.current = resumeIndex(pathRef.current, path, index.current);
      pathRef.current = path;
    }
    if (prevMode.current !== mode) {
      if (mode === 'failed') shakeStart.current = t;
      prevMode.current = mode;
    }

    const last = path.length - 1;
    if (reduced) index.current = last;
    const [tx, , tz] = path[index.current];
    if (reduced) {
      g.position.x = tx;
      g.position.z = tz;
    } else {
      g.position.x = THREE.MathUtils.damp(g.position.x, tx, MOVE_LAMBDA, delta);
      g.position.z = THREE.MathUtils.damp(g.position.z, tz, MOVE_LAMBDA, delta);
    }
    const dist = Math.hypot(tx - g.position.x, tz - g.position.z);
    index.current = advanceWaypoint(index.current, dist, last, ARRIVE_DIST);

    const animate = !reduced && mode === 'moving';
    const bob = animate ? Math.abs(Math.sin(t * BOB_SPEED)) * BOB_AMP : 0;
    g.position.y = THREE.MathUtils.damp(g.position.y, CUBE_Y + bob, Y_LAMBDA, delta);

    const shakeLeft = reduced ? 0 : Math.max(0, 1 - (t - shakeStart.current) / SHAKE_SECONDS);
    body.current.position.x = Math.sin(t * SHAKE_FREQ) * SHAKE_AMP * shakeLeft;

    const r = body.current.rotation;
    if (animate) r.y += delta * SPIN_SPEED;
    r.x = THREE.MathUtils.damp(r.x, animate ? Math.sin(t * TILT_FREQ_X) * TILT : 0, TILT_LAMBDA, delta);
    r.z = THREE.MathUtils.damp(r.z, animate ? Math.cos(t * TILT_FREQ_Z) * TILT : 0, TILT_LAMBDA, delta);

    const tone = TONE[cubeTone(mode, station)];
    const k = 1 - Math.exp(-COLOR_LAMBDA * delta);
    m.color.lerp(tone, k);
    m.emissive.lerp(tone, k);
    const healPulse = mode === 'moving' && station === 'heal' && !reduced ? HEAL_PULSE * (1 + Math.sin(t * PULSE_SPEED)) : 0;
    const glow = GLOW[mode] ?? GLOW.moving;
    m.emissiveIntensity = THREE.MathUtils.damp(m.emissiveIntensity, glow + healPulse, GLOW_LAMBDA, delta);
  });

  return (
    <group ref={group}>
      <group ref={body}>
        <RoundedBox args={[CUBE_SIZE, CUBE_SIZE, CUBE_SIZE]} radius={0.08} smoothness={4}>
          <meshStandardMaterial ref={material} color={TONE.slate} emissive={TONE.slate} emissiveIntensity={0.2} roughness={0.35} metalness={0.25} />
        </RoundedBox>
      </group>
      <Html position={[0, CUBE_SIZE * 0.95, 0]} center zIndexRange={HTML_Z} pointerEvents="none" portal={portal}>
        <span className="pointer-events-none select-none rounded bg-slate-950/80 px-1 font-mono text-[10px] font-bold text-violet-200 ring-1 ring-violet-500/40">
          {'</>'}
        </span>
      </Html>
    </group>
  );
}

function JourneyScene({ steps, journey, path, reduced, portal }) {
  const running = journey.mode === 'moving';
  const detourLit = journey.healed || journey.station === 'heal';
  return (
    <>
      <CameraRig />
      <ambientLight intensity={0.45} />
      <directionalLight position={[4, 8, 5]} intensity={1.1} />
      <pointLight position={[0, 3, 2]} intensity={6} color="#8b5cf6" distance={12} />
      <mesh rotation-x={-Math.PI / 2} position={[0, -0.01, 0.6]}>
        <planeGeometry args={[14, 6]} />
        <meshStandardMaterial color="#0b1020" roughness={1} />
      </mesh>
      {TRACK_SEGMENTS.map((seg) => (
        <TrackSegment key={`${seg.from}-${seg.to}`} segment={seg} lit={seg.detour ? detourLit : journey.mode !== 'idle'} running={running} reduced={reduced} />
      ))}
      <StationPad position={START_PAD.position} label={START_PAD.label} state={journey.mode === 'idle' ? 'active' : 'done'} reduced={reduced} portal={portal} />
      {STATIONS.map((s) => (
        <StationPad key={s.id} position={s.position} label={s.label} state={steps[s.id]} celebrate={s.id === 'live' && journey.mode === 'done'} reduced={reduced} portal={portal} />
      ))}
      <CodeCube path={path} mode={journey.mode} station={journey.station} reduced={reduced} portal={portal} />
    </>
  );
}

export default function PipelineJourney({ run, className = '', height = 220 }) {
  const reduced = useReducedMotion();
  // Html labels portal here: without a fixed target drei re-mounts every label root once R3F connects its events
  const labelLayer = useRef(null);
  const journey = useMemo(() => journeyState(run), [run]);
  const steps = useMemo(() => (run ? stepStatus(run) : { analyze: 'idle', deploy: 'idle', heal: 'idle', live: 'idle' }), [run]);
  const path = useMemo(() => pathTo(journey.station, journey.healed), [journey.station, journey.healed]);
  const summary = `배포 여정: ${STATION_LABEL[journey.station]} · ${MODE_LABEL[journey.mode]}${journey.healed ? ' · 자가치유 경유' : ''}`;

  return (
    <div className={`rounded-2xl border border-white/5 bg-slate-900/60 p-4 shadow-xl shadow-black/20 ${className}`}>
      <div className="mb-2 flex items-center justify-between gap-2 px-1">
        <span className="text-sm font-semibold text-slate-200">배포 여정</span>
        <div className="flex items-center gap-2 text-xs">
          {journey.healed && <span className="rounded-md border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 text-amber-200">자가치유 경유</span>}
          <span className={`font-medium ${MODE_BADGE[journey.mode]}`}>{MODE_LABEL[journey.mode]}</span>
        </div>
      </div>
      <div ref={labelLayer} role="img" aria-label={summary} className="relative isolate overflow-hidden rounded-xl bg-[#0a0d14]/60" style={{ height }}>
        <Canvas dpr={[1, 2]} camera={CAMERA} gl={GL}>
          <Suspense fallback={null}>
            <JourneyScene steps={steps} journey={journey} path={path} reduced={reduced} portal={labelLayer} />
          </Suspense>
        </Canvas>
      </div>
    </div>
  );
}
