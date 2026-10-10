import React, { Component, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { Canvas, useFrame } from '@react-three/fiber';
import { ContactShadows, Sparkles, useAnimations, useGLTF } from '@react-three/drei';
import * as THREE from 'three';
import { ExternalLink, Wrench, X } from 'lucide-react';
import { summarizeHeal } from '../../lib/victory';

const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';
const REDUCED_MOTION_SCALE = 0.25;

const STAGE_RADIUS = 2.2;
const STAGE_HEIGHT = 0.3;
const SPOT_TARGET = [0, 1, 0];
const KEY_LIGHT_INTENSITY = 6;
const FILL_LIGHT_INTENSITY = 3;
const RIM_LIGHT_INTENSITY = 1.5;
const SHADOW_MAP_SIZE = 1024;

const DANCER_HEIGHT = 2;
const CLIP_FADE_SEC = 0.4;
const DANCER_REDUCED_SPEED = 0.5;

const POP_IN_LAMBDA = 6;
const ARM_LAMBDA = 10;
const JUMP_FREQ = 4;
const JUMP_HEIGHT = 0.35;
const WAVE_FREQ = 7;
const ARM_RAISE = 2.4;
const ARM_SWING = 0.45;
const SPIN_PERIOD_SEC = 5;
const SPIN_DURATION_SEC = 1.2;

const CLOUD_COLOR = '#f1f5ff';
const CLOUD_GLOW = '#7c3aed';
const PUFFS = [
  { pos: [0, 0.95, 0], r: 0.58 },
  { pos: [-0.5, 0.82, 0], r: 0.42 },
  { pos: [0.5, 0.82, 0], r: 0.42 },
  { pos: [-0.24, 1.35, 0.02], r: 0.38 },
  { pos: [0.26, 1.38, -0.04], r: 0.36 },
  { pos: [0, 0.9, -0.35], r: 0.45 },
];

const SPARKLE_LAYERS = [
  { color: '#a78bfa', count: 70, size: 4, speed: 0.45, scale: [6, 4, 6], position: [0, 2, 0] },
  { color: '#34d399', count: 45, size: 3, speed: 0.6, scale: [5, 3.5, 5], position: [0, 1.8, 0] },
  { color: '#fbbf24', count: 30, size: 6, speed: 0.3, scale: [3, 3, 3], position: [0, 2.2, 0] },
  { color: '#e2e8f0', count: 90, size: 1.5, speed: 0.2, scale: [11, 6, 11], position: [0, 2.5, -1] },
];

// Fitting walks the whole scene graph, and useGLTF hands back the same cached scene on every open.
const FIT_CACHE = new WeakMap();

function fitToHeight(scene) {
  if (FIT_CACHE.has(scene)) return FIT_CACHE.get(scene);
  const box = new THREE.Box3().setFromObject(scene);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const scale = size.y > 0 ? DANCER_HEIGHT / size.y : 1;
  const fit = { scale, position: [-center.x * scale, -box.min.y * scale, -center.z * scale] };
  FIT_CACHE.set(scene, fit);
  return fit;
}

const pickAction = (actions, names, clip) => actions[clip] || (names[0] ? actions[names[0]] : null);

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

const smoothstep = (x) => x * x * (3 - 2 * x);

function Puff({ pos, r }) {
  return (
    <mesh position={pos} castShadow>
      <sphereGeometry args={[r, 32, 32]} />
      <meshStandardMaterial color={CLOUD_COLOR} emissive={CLOUD_GLOW} emissiveIntensity={0.08} roughness={0.55} />
    </mesh>
  );
}

function Arm({ armRef, side }) {
  return (
    <group ref={armRef} position={[side * 0.78, 0.92, 0]}>
      <mesh position={[0, 0.28, 0]} castShadow>
        <capsuleGeometry args={[0.08, 0.38, 4, 12]} />
        <meshStandardMaterial color={CLOUD_COLOR} roughness={0.6} />
      </mesh>
      <mesh position={[0, 0.58, 0]} castShadow>
        <sphereGeometry args={[0.13, 20, 20]} />
        <meshStandardMaterial color="#c4b5fd" emissive="#8b5cf6" emissiveIntensity={0.4} />
      </mesh>
    </group>
  );
}

function CloudBot({ reduced = false }) {
  const root = useRef();
  const body = useRef();
  const leftArm = useRef();
  const rightArm = useRef();

  useFrame((state, delta) => {
    const t = state.clock.elapsedTime;
    const motion = reduced ? REDUCED_MOTION_SCALE : 1;
    const pop = THREE.MathUtils.damp(root.current.scale.x, 1, POP_IN_LAMBDA, delta);
    root.current.scale.setScalar(pop);

    const hop = Math.abs(Math.sin(t * JUMP_FREQ));
    body.current.position.y = hop * JUMP_HEIGHT * motion;
    // Squash a little at the bottom of each hop so the landing reads as weight.
    body.current.scale.y = 1 - (1 - hop) * 0.08 * motion;

    const spinPhase = (t % SPIN_PERIOD_SEC) / SPIN_DURATION_SEC;
    const spin = !reduced && spinPhase < 1 ? smoothstep(spinPhase) * Math.PI * 2 : 0;
    root.current.rotation.y = Math.sin(t * 0.9) * 0.35 * motion + spin;

    const wave = Math.sin(t * WAVE_FREQ) * ARM_SWING * motion;
    leftArm.current.rotation.z = THREE.MathUtils.damp(leftArm.current.rotation.z, ARM_RAISE * 0.5 + wave, ARM_LAMBDA, delta);
    rightArm.current.rotation.z = THREE.MathUtils.damp(rightArm.current.rotation.z, -ARM_RAISE * 0.5 - wave, ARM_LAMBDA, delta);
  });

  return (
    <group ref={root} scale={0.01}>
      <group ref={body}>
        {PUFFS.map((p, i) => <Puff key={i} {...p} />)}
        {[-1, 1].map((side) => (
          <group key={side} position={[side * 0.19, 1.02, 0.52]}>
            <mesh>
              <sphereGeometry args={[0.085, 16, 16]} />
              <meshStandardMaterial color="#0f172a" roughness={0.2} />
            </mesh>
            <mesh position={[0.025, 0.03, 0.07]}>
              <sphereGeometry args={[0.025, 8, 8]} />
              <meshBasicMaterial color="#67e8f9" />
            </mesh>
          </group>
        ))}
        <mesh position={[0, 0.84, 0.55]} rotation={[0, 0, Math.PI]}>
          <torusGeometry args={[0.12, 0.022, 8, 20, Math.PI]} />
          <meshStandardMaterial color="#0f172a" />
        </mesh>
        <mesh position={[0.02, 1.86, 0]}>
          <cylinderGeometry args={[0.02, 0.02, 0.3, 8]} />
          <meshStandardMaterial color="#94a3b8" metalness={0.6} roughness={0.3} />
        </mesh>
        <mesh position={[0.02, 2.04, 0]}>
          <sphereGeometry args={[0.08, 16, 16]} />
          <meshStandardMaterial color="#fbbf24" emissive="#f59e0b" emissiveIntensity={1.5} />
        </mesh>
        <Arm armRef={leftArm} side={-1} />
        <Arm armRef={rightArm} side={1} />
        {[-1, 1].map((side) => (
          <mesh key={side} position={[side * 0.22, 0.24, 0]} castShadow>
            <capsuleGeometry args={[0.09, 0.18, 4, 12]} />
            <meshStandardMaterial color="#cbd5e1" roughness={0.6} />
          </mesh>
        ))}
      </group>
    </group>
  );
}

function GltfDancer({ url, clip, reduced }) {
  const group = useRef();
  const { scene, animations } = useGLTF(url);
  const { actions, names } = useAnimations(animations, group);
  // Never transform the cached scene itself: a wrapper group keeps reopen from compounding the fit.
  const fit = useMemo(() => fitToHeight(scene), [scene]);

  useEffect(() => {
    scene.traverse((o) => {
      if (o.isMesh) o.castShadow = true;
    });
  }, [scene]);

  // drei only builds actions once the root ref is attached, so resolve them after commit, not during render.
  useEffect(() => {
    const action = pickAction(actions, names, clip);
    if (!action) return undefined;
    action.reset().fadeIn(CLIP_FADE_SEC).play();
    return () => {
      action.fadeOut(CLIP_FADE_SEC);
    };
  }, [actions, names, clip]);

  useEffect(() => {
    pickAction(actions, names, clip)?.setEffectiveTimeScale(reduced ? DANCER_REDUCED_SPEED : 1);
  }, [actions, names, clip, reduced]);

  return (
    <group ref={group} dispose={null}>
      <group scale={fit.scale} position={fit.position}>
        <primitive object={scene} />
      </group>
    </group>
  );
}

class ModelBoundary extends Component {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

function Performer({ modelUrl, clip, reduced }) {
  const fallback = <CloudBot reduced={reduced} />;
  if (!modelUrl) return fallback;
  return (
    <ModelBoundary key={modelUrl} fallback={fallback}>
      <Suspense fallback={fallback}>
        <GltfDancer url={modelUrl} clip={clip} reduced={reduced} />
      </Suspense>
    </ModelBoundary>
  );
}

function VictoryStage({ modelUrl, clip, reduced }) {
  const keyLight = useRef();
  const fillLight = useRef();
  // A spotlight only aims at its target if that target lives in the scene graph (so its matrix updates).
  const spotTarget = useMemo(() => new THREE.Object3D(), []);

  useEffect(() => {
    keyLight.current.target = spotTarget;
    fillLight.current.target = spotTarget;
  }, [spotTarget]);

  const sparkleSpeed = reduced ? REDUCED_MOTION_SCALE : 1;

  return (
    <>
      <ambientLight intensity={0.25} />
      <primitive object={spotTarget} position={SPOT_TARGET} />
      <spotLight
        ref={keyLight}
        position={[2.5, 6, 3.5]}
        angle={0.42}
        penumbra={0.6}
        intensity={KEY_LIGHT_INTENSITY}
        decay={0}
        castShadow
        shadow-mapSize={[SHADOW_MAP_SIZE, SHADOW_MAP_SIZE]}
        shadow-bias={-0.0005}
      />
      <spotLight ref={fillLight} position={[-3.5, 5, -1.5]} angle={0.5} penumbra={0.9} intensity={FILL_LIGHT_INTENSITY} decay={0} color="#a78bfa" />
      <pointLight position={[-2.5, 1.5, -2]} intensity={RIM_LIGHT_INTENSITY} decay={0} distance={6} color="#8b5cf6" />
      <pointLight position={[2.5, 1.2, -2]} intensity={RIM_LIGHT_INTENSITY} decay={0} distance={6} color="#10b981" />

      <mesh position={[0, -STAGE_HEIGHT / 2, 0]} receiveShadow>
        <cylinderGeometry args={[STAGE_RADIUS, STAGE_RADIUS + 0.15, STAGE_HEIGHT, 64]} />
        <meshStandardMaterial color="#1e1b4b" metalness={0.4} roughness={0.45} />
      </mesh>
      <mesh position={[0, 0.002, 0]} rotation={[-Math.PI / 2, 0, 0]}>
        <ringGeometry args={[STAGE_RADIUS - 0.08, STAGE_RADIUS, 64]} />
        <meshBasicMaterial color="#a78bfa" toneMapped={false} />
      </mesh>
      <mesh position={[0, -STAGE_HEIGHT - 0.1, 0]} receiveShadow>
        <cylinderGeometry args={[STAGE_RADIUS + 0.5, STAGE_RADIUS + 0.6, 0.2, 64]} />
        <meshStandardMaterial color="#0f172a" metalness={0.2} roughness={0.8} />
      </mesh>
      <ContactShadows position={[0, 0.01, 0]} opacity={0.55} scale={STAGE_RADIUS * 2.2} blur={2.4} far={3} resolution={512} color="#000000" />

      <Performer modelUrl={modelUrl} clip={clip} reduced={reduced} />

      {SPARKLE_LAYERS.map((s) => (
        <Sparkles key={s.color} count={s.count} size={s.size} speed={s.speed * sparkleSpeed} scale={s.scale} position={s.position} color={s.color} noise={1} />
      ))}
    </>
  );
}

function HealSummary({ run, summary }) {
  return (
    <div className="space-y-2.5 border-t border-white/5 px-5 py-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-1.5 rounded-md bg-emerald-500/10 px-2 py-0.5 text-xs font-semibold text-emerald-300 ring-1 ring-inset ring-emerald-500/30">
          <Wrench className="h-3.5 w-3.5" /> AI 자가치유 성공
        </span>
        {run?.id && <span className="font-mono text-xs text-slate-500">#{run.id}</span>}
      </div>
      <p className="text-lg font-semibold text-slate-100">
        {summary.patchCount > 0 ? `패치 ${summary.patchCount}개로 배포 실패를 스스로 고쳤어요` : '실패한 배포를 스스로 고쳐 다시 올렸어요'}
      </p>
      {summary.categories.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {summary.categories.map((c) => (
            <span key={c} className="rounded-md bg-amber-500/10 px-1.5 py-0.5 font-mono text-[11px] text-amber-200 ring-1 ring-inset ring-amber-500/30">{c}</span>
          ))}
        </div>
      )}
      {summary.urls.length > 0 && (
        <ul className="space-y-1">
          {summary.urls.map((u) => (
            <li key={u}>
              <a href={u} target="_blank" rel="noopener noreferrer"
                className="inline-flex max-w-full items-center gap-1.5 truncate text-sm text-violet-300 underline-offset-4 hover:text-violet-200 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-400">
                <ExternalLink className="h-3.5 w-3.5 shrink-0" /> <span className="truncate">공개 URL · {u}</span>
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// All hooks live here so the default export can bail out before any of them run.
function Overlay({ run, onClose, modelUrl, clip, autoCloseMs, className, height }) {
  const closeButton = useRef(null);
  // App re-renders on every tick with a fresh onClose; reading it through a ref keeps the timer from resetting.
  const onCloseRef = useRef(onClose);
  const reduced = usePrefersReducedMotion();
  const summary = useMemo(() => summarizeHeal(run), [run]);

  useEffect(() => {
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    closeButton.current?.focus();
    const onKey = (e) => {
      if (e.key === 'Escape') onCloseRef.current?.();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  useEffect(() => {
    if (!(autoCloseMs > 0)) return undefined;
    const id = setTimeout(() => onCloseRef.current?.(), autoCloseMs);
    return () => clearTimeout(id);
  }, [autoCloseMs]);

  return (
    <div className={`pointer-events-none fixed inset-0 z-50 flex items-center justify-center p-4 ${className}`}>
      <div aria-hidden="true" className="absolute inset-0 bg-[#0a0d14]/50" />
      <section role="dialog" aria-label="AI 자가치유 성공 축하"
        className="pointer-events-auto relative w-full max-w-3xl overflow-hidden rounded-2xl border border-white/5 bg-slate-900/80 shadow-2xl shadow-violet-900/40 backdrop-blur">
        <button ref={closeButton} type="button" aria-label="닫기" onClick={() => onCloseRef.current?.()}
          className="pointer-events-auto absolute right-3 top-3 z-10 rounded-lg border border-white/10 bg-slate-900/70 p-1.5 text-slate-400 transition-colors hover:border-white/25 hover:text-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-400 active:scale-95">
          <X className="h-4 w-4" />
        </button>
        <div className="relative bg-gradient-to-b from-violet-950/40 via-transparent to-transparent" style={{ height }}>
          <Canvas shadows="percentage" dpr={[1, 2]} camera={{ position: [0, 2.3, 6.4], fov: 40 }} onCreated={({ camera }) => camera.lookAt(...SPOT_TARGET)}>
            <Suspense fallback={null}>
              <VictoryStage modelUrl={modelUrl} clip={clip} reduced={reduced} />
            </Suspense>
          </Canvas>
        </div>
        <HealSummary run={run} summary={summary} />
      </section>
    </div>
  );
}

export default function VictoryCelebration({
  open,
  run,
  onClose,
  modelUrl = import.meta.env.VITE_VICTORY_MODEL_URL,
  clip = 'hiphop01',
  autoCloseMs = 0,
  className = '',
  height = 380,
}) {
  if (!open) return null;
  return <Overlay run={run} onClose={onClose} modelUrl={modelUrl} clip={clip} autoCloseMs={autoCloseMs} className={className} height={height} />;
}
