import React, { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react';
import { Boxes, Cloud, KeyRound } from 'lucide-react';
import { answerQuestion, cancelDeploy, getDeployment, getFleet, getHealth, getProjects, getToken, setToken, startDeploy, stopProject, submitEnv, subscribe } from './api';
import { applyEvent, emptyRun } from './lib/run';
import DeployForm from './components/DeployForm';
import EnvRequestCard from './components/EnvRequestCard';
import Stepper from './components/Stepper';
import LogConsole from './components/LogConsole';
import { AnalysisCard, Endpoints, PatchList, QuestionCard } from './components/ResultPanels';
import { FleetPanel, ProjectsPanel } from './components/OpsPanels';
import { Card, Dot } from './components/ui';
import SceneBoundary from './components/3d/SceneBoundary';
import { shouldCelebrate } from './lib/victory';

// three / fiber / drei live in lazy chunks so the 2D dashboard paints first
const PipelineJourney = lazy(() => import('./components/3d/PipelineJourney'));
const InstancedFleetGrid = lazy(() => import('./components/3d/InstancedFleetGrid'));
const VictoryCelebration = lazy(() => import('./components/3d/VictoryCelebration'));

const JOURNEY_HEIGHT = 220;
const FLEET_3D_HEIGHT = 320;
const FLEET_DEMO_MAX = 500;

// ?fleetDemo=N adds N synthetic nodes to the 3D fleet grid (instancing demo)
function parseFleetDemo(search) {
  const n = Number.parseInt(new URLSearchParams(search).get('fleetDemo') ?? '', 10);
  return Number.isFinite(n) ? Math.min(FLEET_DEMO_MAX, Math.max(0, n)) : 0;
}

// same chrome and height as the loaded component, so nothing shifts when the chunk arrives
function JourneySkeleton() {
  return (
    <div className="rounded-2xl border border-white/5 bg-slate-900/60 p-4 shadow-xl shadow-black/20">
      <div className="mb-2 h-5 px-1 text-sm font-semibold text-slate-200">배포 여정</div>
      <div className="animate-pulse rounded-xl bg-[#0a0d14]/60" style={{ height: JOURNEY_HEIGHT }} />
    </div>
  );
}

function FleetSkeleton() {
  return (
    <Card title="노드 풀 3D" icon={Boxes}>
      <div className="animate-pulse rounded-xl border border-white/5 bg-[#0a0d14]" style={{ height: FLEET_3D_HEIGHT }} />
    </Card>
  );
}

export default function App() {
  const [form, setForm] = useState({ source: 'sample-apps/guestbook', name: '', ref: '', targets: { local: true, cloudrun: true, node: false, function: false } });
  const [run, setRun] = useState(() => emptyRun());
  const [projects, setProjects] = useState(null);
  const [fleet, setFleet] = useState(null);
  const [healthy, setHealthy] = useState(null);
  const [token, setTokenState] = useState(getToken());
  const [showToken, setShowToken] = useState(false);
  const [autoFollow, setAutoFollow] = useState(true);
  const [notice, setNotice] = useState(null);
  const [stopping, setStopping] = useState(null);
  const [cancelling, setCancelling] = useState(null);
  const [starting, setStarting] = useState(false);
  const closeRef = useRef(null);
  const seenDeployments = useRef(null);
  const [now, setNow] = useState(() => Date.now());
  const [dismissedVictoryIds, setDismissedVictoryIds] = useState(() => new Set());
  const [fleetDemo] = useState(() => parseFleetDemo(window.location.search));

  // re-render once a second while running so elapsed time moves
  useEffect(() => {
    if (run.status !== 'running') return undefined;
    const h = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(h);
  }, [run.status]);

  const attach = useCallback((id, meta) => {
    closeRef.current?.();
    setRun(emptyRun(id, meta));
    closeRef.current = subscribe(id, (type, p, raw) => setRun((r) => (r.id === id ? applyEvent(r, type, p, raw) : r)));
  }, []);

  // close the stream once every target reported (otherwise EventSource reconnects and replays forever)
  useEffect(() => {
    if (run.id && run.status !== 'running') {
      closeRef.current?.();
      closeRef.current = null;
      getDeployment(run.id).catch(() => {});
    }
  }, [run.id, run.status]);

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      const [h, p] = await Promise.all([getHealth(), getProjects().catch(() => null)]);
      if (!alive) return;
      setHealthy(h);
      if (p) {
        setProjects(p);
        // auto-follow deployments started by a GitHub push
        const prev = seenDeployments.current;
        seenDeployments.current = Object.fromEntries(p.map((x) => [x.name, x.last_deployment_id]));
        if (prev && autoFollow) {
          const fresh = p.find((x) => x.trigger === 'github-push' && x.last_status === 'running' && prev[x.name] !== x.last_deployment_id);
          if (fresh) {
            setNotice(`GitHub push 감지 · ${fresh.name}@${fresh.ref} 배포를 따라갑니다`);
            setTimeout(() => setNotice(null), 6000);
            attach(fresh.last_deployment_id, { source: fresh.source, targets: fresh.targets, name: fresh.name, trigger: 'github-push' });
          }
        }
      }
    };
    poll();
    const h = setInterval(poll, 4000);
    return () => { alive = false; clearInterval(h); };
  }, [autoFollow, attach]);

  useEffect(() => {
    let alive = true;
    const poll = async () => { const f = await getFleet().catch(() => null); if (alive) setFleet(f); };
    poll();
    const h = setInterval(poll, 15000);
    return () => { alive = false; clearInterval(h); };
  }, []);

  const onDeploy = async () => {
    const targets = Object.keys(form.targets).filter((t) => form.targets[t]);
    // lock the button before the request returns, otherwise a double click starts two deployments
    setStarting(true);
    try {
      const id = await startDeploy({ source: form.source.trim(), targets, name: form.name.trim() || undefined, ref: form.ref.trim() || undefined });
      attach(id, { source: form.source, targets, name: form.name || null, trigger: 'api' });
    } catch (e) {
      setRun({ ...emptyRun(), status: 'failed', error: e.message, logs: [{ id: 0, at: new Date(), kind: 'error', stage: 'failed', text: `배포 요청 실패: ${e.message}` }] });
    } finally {
      setStarting(false);
    }
  };

  const flash = (text) => { setNotice(text); setTimeout(() => setNotice(null), 6000); };
  const onStop = async (name) => {
    if (!window.confirm(`${name} 앱을 중지할까요?\n컨테이너 · 터널 · Cloud Run 서비스 · 노드 복제본을 내립니다. (DB 데이터는 남겨둡니다)`)) return;
    setStopping(name);
    try {
      const r = await stopProject(name);
      const failed = Object.entries(r.failed);
      flash(failed.length ? `${name} 일부 중지 실패 · ${failed.map(([t, e]) => `${t}: ${e}`).join(' · ')}` : `${name} 중지됨`);
    } catch (e) {
      flash(`${name} 중지 실패 · ${e.message}`);
    } finally {
      setStopping(null);
      getProjects().then((p) => p && setProjects(p)).catch(() => {});
    }
  };
  const onCancel = async () => {
    const id = run.id;
    if (!window.confirm('진행 중인 배포를 중단할까요?\n이미 떠 있는 타깃은 그대로 두고, 나머지는 시작하지 않습니다.')) return;
    setCancelling(id);
    try {
      await cancelDeploy(id);
    } catch (e) {
      flash(`중단 실패 · ${e.message}`);
      setCancelling(null);
    }
  };
  const currentProject = projects?.find((p) => p.last_deployment_id === run.id) || null;

  const watchProject = (p) => attach(p.last_deployment_id, { source: p.source, targets: p.targets || [], name: p.name, trigger: p.trigger });
  const saveToken = (v) => { setToken(v); setTokenState(v); };
  const celebrate = shouldCelebrate(run, dismissedVictoryIds);

  return (
    <div className="min-h-screen bg-[#0a0d14] text-slate-100">
      <div className="pointer-events-none fixed inset-x-0 top-0 h-72 bg-gradient-to-b from-violet-900/20 via-indigo-900/5 to-transparent" />
      <div className="relative mx-auto max-w-[1400px] px-6 pb-16 pt-6">
        <header className="mb-6 flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="grid h-10 w-10 place-items-center rounded-xl bg-gradient-to-br from-violet-500 to-indigo-600 shadow-lg shadow-violet-900/50">
              <Cloud className="h-5 w-5 text-white" />
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-tight">CloudMorph</h1>
              <p className="text-xs text-slate-400">One Action, Infinite Clouds — 분석 · 배포 · 자가치유 · 멀티 클라우드</p>
            </div>
          </div>
          <div className="flex items-center gap-3 text-xs">
            <span className="flex items-center gap-1.5 rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-slate-300">
              <Dot tone={healthy ? 'emerald' : healthy === false ? 'rose' : 'slate'} pulse={!!healthy} /> 백엔드 {healthy ? '연결됨' : healthy === false ? '끊김' : '확인 중'}
            </span>
            {fleet && <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-slate-300">노드 {fleet.nodes.filter((n) => n.ok).length}/{fleet.nodes.length}</span>}
            <button type="button" onClick={() => setShowToken((s) => !s)} className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 ${token ? 'border-emerald-500/30 text-emerald-300' : 'border-white/10 text-slate-400'} hover:text-white`}>
              <KeyRound className="h-3.5 w-3.5" /> {token ? '토큰 설정됨' : 'API 토큰'}
            </button>
            {showToken && (
              <input autoFocus type="password" value={token} onChange={(e) => saveToken(e.target.value)} placeholder="공개 터널일 때만 필요"
                className="w-52 rounded-lg border border-white/10 bg-black/40 px-2.5 py-1 text-slate-100 focus:border-violet-500/60 focus:outline-none" />
            )}
          </div>
        </header>

        {notice && (
          <div className="mb-4 flex items-center gap-2 rounded-xl border border-sky-500/30 bg-sky-500/10 px-4 py-2.5 text-sm text-sky-100">
            <Dot tone="sky" pulse /> {notice}
          </div>
        )}

        <div className="grid gap-5 xl:grid-cols-[360px_minmax(0,1fr)]">
          <div className="space-y-5">
            <DeployForm form={form} setForm={setForm} onDeploy={onDeploy} busy={starting || run.status === 'running'} fleetAvailable={!!fleet} />
            <AnalysisCard run={run} />
          </div>
          <div className="min-w-0 space-y-5">
            {run.envRequest && <EnvRequestCard key={run.id} request={run.envRequest} onSubmit={(values) => submitEnv(run.id, values)} />}
            <Stepper run={run} now={now} onCancel={onCancel} cancelling={cancelling === run.id} />
            {run.status !== 'idle' && (
              <SceneBoundary>
                <Suspense fallback={<JourneySkeleton />}>
                  <PipelineJourney run={run} height={JOURNEY_HEIGHT} />
                </Suspense>
              </SceneBoundary>
            )}
            <QuestionCard question={run.question} onAnswer={(qid, choice) => answerQuestion(run.id, qid, choice)} />
            <div className="grid gap-5 2xl:grid-cols-[minmax(0,1fr)_380px]">
              <LogConsole logs={run.logs} running={run.status === 'running'} />
              <div className="min-w-0 space-y-5">
                <Endpoints run={run} project={currentProject} onStop={onStop} stopping={stopping} />
                <PatchList patches={run.patches} />
              </div>
            </div>
          </div>
        </div>

        <div className="mt-5 space-y-5">
          <ProjectsPanel projects={projects} currentId={run.id} onWatch={watchProject} onStop={onStop} stopping={stopping} autoFollow={autoFollow} setAutoFollow={setAutoFollow} />
          <FleetPanel fleet={fleet} />
          {fleet && (
            <SceneBoundary>
              <Suspense fallback={<FleetSkeleton />}>
                <InstancedFleetGrid fleet={fleet} demoCount={fleetDemo} height={FLEET_3D_HEIGHT} />
              </Suspense>
            </SceneBoundary>
          )}
        </div>
      </div>

      {/* fixed z-50 overlay: rendered only while open so its chunk is fetched on the first celebration */}
      {celebrate && (
        <SceneBoundary key={run.id}>
          <Suspense fallback={null}>
            <VictoryCelebration open run={run} onClose={() => setDismissedVictoryIds((ids) => new Set(ids).add(run.id))} />
          </Suspense>
        </SceneBoundary>
      )}
    </div>
  );
}
