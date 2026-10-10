import React, { Suspense, useEffect, useMemo, useRef, useState } from 'react';
import * as THREE from 'three';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import { Grid, Html, Instance, Instances, OrbitControls } from '@react-three/drei';
import { Boxes } from 'lucide-react';
import { Card } from '../ui';
import {
  STATUSES,
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
} from '../../lib/fleetStatus';

const SPACING = 1.4;
const RACK = [0.8, 1.2, 0.8];
const RACK_HALF_H = RACK[1] / 2;
const TOOLTIP_LIFT = 0.45;
const DEMO_SEED = 7;
const REDUCED_INTENSITY = 0.25;
const HOVER_GLOW = 0.35;
const CAMERA_DIR = new THREE.Vector3(1, 0.9, 1.25).normalize();
const MIN_ZOOM = 0.4;
const MAX_ZOOM = 1.8;
const MAX_POLAR = Math.PI * 0.44;
const AUTO_ROTATE_SPEED = 0.35;
// Keep drei's tooltip below app overlays (modals use z-50).
const TOOLTIP_Z = [30, 0];
const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';

const WHITE = new THREE.Color('white');
const BLACK = new THREE.Color('black');
// Brighten within the same hue: lerping toward white desaturates orange into salmon and blurs the status colours.
const HIGHLIGHT_LIGHTNESS = 0.08;
const HIGHLIGHT_GAIN = 2;

function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(() => typeof window !== 'undefined' && !!window.matchMedia?.(REDUCED_MOTION_QUERY).matches);
  useEffect(() => {
    const mq = window.matchMedia?.(REDUCED_MOTION_QUERY);
    if (!mq) return undefined;
    const onChange = (e) => setReduced(e.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);
  return reduced;
}

function FleetInstances({ nodes, positions, reduced, hovered, setHovered }) {
  const refs = useRef([]);
  const elapsed = useRef(0);
  const statuses = useMemo(() => nodes.map(classifyNode), [nodes]);
  const bases = useMemo(() => statuses.map((s) => new THREE.Color(STATUS_STYLE[s].color)), [statuses]);
  const highlights = useMemo(() => bases.map((c) => c.clone().offsetHSL(0, 0, HIGHLIGHT_LIGHTNESS)), [bases]);
  const capacity = instanceCapacity(nodes.length);

  // One loop drives every instance; only the virtual instance objects are mutated, never React state.
  useFrame((_, delta) => {
    elapsed.current += delta;
    const intensity = reduced ? REDUCED_INTENSITY : 1;
    for (let i = 0; i < statuses.length; i++) {
      const obj = refs.current[i];
      if (!obj) continue;
      const { scaleY, mix } = pulseAt(statuses[i], elapsed.current, phaseFor(i), intensity);
      obj.scale.y = scaleY;
      obj.position.y = RACK_HALF_H * scaleY; // grow upward from the floor, not around the centre
      if (mix >= 0) obj.color.copy(bases[i]).lerp(highlights[i], Math.min(1, mix * HIGHLIGHT_GAIN));
      else obj.color.copy(bases[i]).lerp(BLACK, -mix);
      if (i === hovered) obj.color.lerp(WHITE, HOVER_GLOW);
    }
  });

  return (
    <Instances key={capacity} limit={capacity} range={nodes.length}>
      <boxGeometry args={RACK} />
      {/* Untone-mapped so cube colours match the legend's exact status hex values. */}
      <meshStandardMaterial color="white" roughness={0.4} metalness={0.15} toneMapped={false} />
      {nodes.map((n, i) => (
        <Instance
          key={`${n.node}-${i}`}
          ref={(el) => { refs.current[i] = el; }}
          position={[positions[i][0], RACK_HALF_H, positions[i][2]]}
          color={STATUS_STYLE[statuses[i]].color}
          onPointerOver={(e) => { e.stopPropagation(); setHovered(i); }}
          onPointerOut={(e) => { e.stopPropagation(); setHovered((h) => (h === i ? null : h)); }}
        />
      ))}
    </Instances>
  );
}

function NodeTooltip({ node, position }) {
  const status = classifyNode(node);
  const style = STATUS_STYLE[status];
  const load = Math.round(loadRatio(node) * 100);
  return (
    <Html position={[position[0], RACK[1] + TOOLTIP_LIFT, position[2]]} center zIndexRange={TOOLTIP_Z} pointerEvents="none">
      <div className="pointer-events-none whitespace-nowrap rounded-lg border border-white/10 bg-slate-950/90 px-2.5 py-1.5 text-[11px] text-slate-300 shadow-xl shadow-black/40 backdrop-blur">
        <div className="font-mono text-[12px] font-semibold text-slate-100">{node.node}</div>
        <div className="mt-0.5 flex items-center gap-2">
          <span className="inline-flex items-center gap-1">
            <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: style.color }} />
            {style.label}
          </span>
          <span className="text-slate-500">·</span>
          <span className="font-mono tabular-nums">{node.ok ? `부하 ${load}%` : '접속 불가'}</span>
        </div>
      </div>
    </Html>
  );
}

function CameraRig({ distance, autoRotate }) {
  const camera = useThree((s) => s.camera);
  const controls = useThree((s) => s.controls);
  useEffect(() => {
    camera.position.copy(CAMERA_DIR).multiplyScalar(distance);
    camera.lookAt(0, 0, 0);
    if (controls) {
      controls.target.set(0, 0, 0);
      controls.update();
    }
  }, [camera, controls, distance]);
  return (
    <OrbitControls
      makeDefault
      enableDamping
      dampingFactor={0.08}
      enablePan={false}
      enableZoom={false} // the card sits in a scrolling page; the wheel must scroll it, not zoom
      minDistance={distance * MIN_ZOOM}
      maxDistance={distance * MAX_ZOOM}
      maxPolarAngle={MAX_POLAR}
      autoRotate={autoRotate}
      autoRotateSpeed={AUTO_ROTATE_SPEED}
    />
  );
}

function FleetScene({ nodes, reduced }) {
  const [hovered, setHovered] = useState(null);
  const positions = useMemo(() => gridLayout(nodes.length, SPACING), [nodes.length]);
  const distance = cameraDistance(nodes.length, SPACING);
  const hoveredNode = hovered != null ? nodes[hovered] : null;

  return (
    <>
      <fog attach="fog" args={['#0a0d14', distance * 1.4, distance * 3.2]} />
      <hemisphereLight args={['#c7d2fe', '#0a0d14', 0.7]} />
      <directionalLight position={[6, 10, 4]} intensity={1.4} />
      <directionalLight position={[-6, 3, -8]} intensity={0.5} color="#8b5cf6" />
      <Grid
        position={[0, -0.001, 0]}
        cellSize={SPACING / 2}
        sectionSize={SPACING * 2}
        cellColor="#1e293b"
        sectionColor="#4c1d95"
        cellThickness={0.6}
        sectionThickness={1}
        fadeDistance={distance * 2.2}
        fadeStrength={1.5}
        infiniteGrid
      />
      <FleetInstances nodes={nodes} positions={positions} reduced={reduced} hovered={hovered} setHovered={setHovered} />
      {hoveredNode && positions[hovered] && <NodeTooltip node={hoveredNode} position={positions[hovered]} />}
      <CameraRig distance={distance} autoRotate={!reduced && hovered == null} />
    </>
  );
}

function Legend({ counts }) {
  return (
    <div className="pointer-events-none absolute bottom-3 left-3 flex flex-wrap gap-1.5">
      {STATUSES.map((s) => (
        <span key={s} className="inline-flex items-center gap-1.5 rounded-md bg-slate-950/70 px-2 py-1 text-[11px] text-slate-300 ring-1 ring-inset ring-white/10 backdrop-blur">
          <span className="h-2 w-2 rounded-full" style={{ backgroundColor: STATUS_STYLE[s].color }} />
          {STATUS_STYLE[s].label}
          <span className="font-mono tabular-nums text-slate-400">{counts[s]}</span>
        </span>
      ))}
    </div>
  );
}

export default function InstancedFleetGrid({ fleet, demoCount = 0, className = '', height = 320 }) {
  const reduced = usePrefersReducedMotion();
  const liveNodes = fleet?.nodes;
  const nodes = useMemo(() => {
    const live = Array.isArray(liveNodes) ? liveNodes : [];
    return demoCount > 0 ? [...live, ...demoNodes(demoCount, DEMO_SEED)] : live;
  }, [liveNodes, demoCount]);
  const counts = useMemo(() => countByStatus(nodes), [nodes]);
  const demo = demoCount > 0 ? Math.floor(demoCount) : 0;

  return (
    <Card
      title="노드 풀 3D"
      icon={Boxes}
      className={className}
      right={
        <span className="text-[11px] text-slate-500">
          노드 <span className="font-mono tabular-nums text-slate-300">{nodes.length}</span>개
          {demo > 0 && <span className="text-amber-300/80"> · 데모 {demo}개 포함</span>}
        </span>
      }
    >
      <div className="relative overflow-hidden rounded-xl border border-white/5 bg-[#0a0d14]" style={{ height }}>
        {nodes.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-1 text-center">
            <Boxes className="h-6 w-6 text-slate-600" />
            <p className="text-sm text-slate-500">등록된 노드가 없습니다</p>
            <p className="text-[11px] text-slate-600">노드가 연결되면 이곳에 3D로 표시됩니다</p>
          </div>
        ) : (
          <>
            <Canvas
              dpr={[1, 2]}
              camera={{ fov: 45, near: 0.1, far: 500, position: CAMERA_DIR.clone().multiplyScalar(cameraDistance(nodes.length, SPACING)).toArray() }}
              gl={{ antialias: true, alpha: true }}
              aria-label={`노드 ${nodes.length}개 3D 상태 그리드`}
            >
              <Suspense fallback={null}>
                <FleetScene nodes={nodes} reduced={reduced} />
              </Suspense>
            </Canvas>
            <Legend counts={counts} />
            <span className="pointer-events-none absolute right-3 top-3 rounded-md bg-slate-950/70 px-2 py-1 font-mono text-[10px] text-violet-300/90 ring-1 ring-inset ring-violet-500/20">
              단일 드로우콜 · Instanced
            </span>
          </>
        )}
      </div>
    </Card>
  );
}
