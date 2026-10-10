import React, { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react';
import { answerQuestion, cancelDeploy, getDeployment, getFleet, getHealth, getProjects, getToken, setToken, startDeploy, stopProject, submitEnv, subscribe } from './api';
import { applyEvent, emptyRun } from './lib/run';
import DeployForm from './components/DeployForm';
import EnvRequestCard from './components/EnvRequestCard';
import LogConsole from './components/LogConsole';
import { AnalysisCard, Endpoints, PatchList, QuestionCard } from './components/ResultPanels';
import { FleetPanel, ProjectsPanel } from './components/OpsPanels';
import { DashboardHeader, RunHUD, ActivityDock } from './components/DashboardChrome';
import DetailsDrawer from './components/DetailsDrawer';
import { pendingInput } from './lib/dashboard';
import SceneBoundary from './components/3d/SceneBoundary';
import { shouldCelebrate } from './lib/victory';

// three / fiber / drei live in lazy chunks so the 2D dashboard paints first
const PipelineJourney = lazy(() => import('./components/3d/PipelineJourney'));
const InstancedFleetGrid = lazy(() => import('./components/3d/InstancedFleetGrid'));
const VictoryCelebration = lazy(() => import('./components/3d/VictoryCelebration'));

const FLEET_DEMO_MAX = 500;

// ?fleetDemo=N adds N synthetic nodes to the 3D fleet grid (instancing demo)
function parseFleetDemo(search) {
  const n = Number.parseInt(new URLSearchParams(search).get('fleetDemo') ?? '', 10);
  return Number.isFinite(n) ? Math.min(FLEET_DEMO_MAX, Math.max(0, n)) : 0;
}

// same chrome and height as the loaded component, so nothing shifts when the chunk arrives
function SceneMessage({ children }) {
  return <div className="scene-message" role="status">{children}</div>;
}

export default function App() {
  const [form, setForm] = useState({ source: 'sample-apps/guestbook', name: '', ref: '', targets: { local: true, cloudrun: true, node: false, function: false } });
  const [run, setRun] = useState(() => emptyRun());
  const [projects, setProjects] = useState(null);
  const [fleet, setFleet] = useState(null);
  const [healthy, setHealthy] = useState(null);
  const [token, setTokenState] = useState(getToken());
  const [activeScene, setActiveScene] = useState('journey');
  const [panel, setPanel] = useState(null);
  const [fullscreen, setFullscreen] = useState(false);
  const shell = useRef(null);
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
  const pending = pendingInput(run);
  const [lastInputKey, setLastInputKey] = useState(null);
  if ((pending?.key || null) !== lastInputKey) {
    setLastInputKey(pending?.key || null);
    if (pending) setPanel('details');
  }

  useEffect(() => {
    const update = () => setFullscreen(document.fullscreenElement === shell.current);
    document.addEventListener('fullscreenchange', update);
    return () => document.removeEventListener('fullscreenchange', update);
  }, []);

  useEffect(() => () => closeRef.current?.(), []);

  // re-render once a second while running so elapsed time moves
  useEffect(() => {
    if (run.status !== 'running') return undefined;
    const h = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(h);
  }, [run.status]);

  const attach = useCallback((id, meta) => {
    closeRef.current?.();
    setActiveScene('journey');
    setPanel(null);
    setCancelling(null);
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

  const dismissVictory = () => setDismissedVictoryIds((ids) => new Set(ids).add(run.id));
  const selectScene = (scene) => {
    if (celebrate) dismissVictory();
    setActiveScene(scene);
  };
  const toggleFullscreen = async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await shell.current?.requestFullscreen?.();
    } catch {
      flash('전체화면을 열지 못했습니다. 현재 화면에서 계속 사용할 수 있습니다.');
    }
  };

  return (
    <div ref={shell} className="dashboard-shell">
      <div className="dashboard-page" inert={panel ? true : undefined}>
        <DashboardHeader scene={activeScene} onScene={selectScene} healthy={healthy} onPanel={setPanel} fullscreen={fullscreen} onFullscreen={toggleFullscreen} />
        <main className="dashboard-stage">
          <RunHUD run={run} now={now} notice={notice} onPanel={setPanel} onCancel={onCancel} cancelling={cancelling === run.id} celebrate={celebrate} />
          <div id="scene-view" className="scene-view" role="tabpanel" aria-labelledby={`scene-tab-${activeScene}`}>
            <SceneBoundary key={celebrate ? `victory-${run.id}` : activeScene} fallback={<SceneMessage>3D 화면을 사용할 수 없습니다. 상태와 상세 패널에서 배포를 계속 확인할 수 있습니다.</SceneMessage>}>
              <Suspense fallback={<SceneMessage>3D 장면을 준비하고 있습니다…</SceneMessage>}>
                {celebrate ? <VictoryCelebration open run={run} presentation="stage" onClose={dismissVictory} />
                  : activeScene === 'journey' ? <PipelineJourney run={run} presentation="stage" />
                    : fleet ? <InstancedFleetGrid fleet={fleet} demoCount={fleetDemo} presentation="stage" />
                      : <SceneMessage>노드 풀 데이터를 불러오지 못했습니다. 연결 상태와 설정을 확인해 주세요.</SceneMessage>}
              </Suspense>
            </SceneBoundary>
          </div>
          <ActivityDock run={run} project={currentProject} onPanel={setPanel} />
        </main>
      </div>
      <DetailsDrawer panel={panel} onPanel={setPanel} onClose={() => setPanel(null)}>
        <div className="drawer-section" hidden={panel !== 'deploy'}>
          <DeployForm form={form} setForm={setForm} onDeploy={onDeploy} busy={starting || run.status === 'running'} fleetAvailable={!!fleet} />
        </div>
        <div className="drawer-section" hidden={panel !== 'details'}>
          {run.envRequest && <EnvRequestCard key={`env-${run.id}`} request={run.envRequest} onSubmit={(values) => submitEnv(run.id, values)} />}
          <QuestionCard key={`question-${run.id}`} question={run.question} onAnswer={(qid, choice) => answerQuestion(run.id, qid, choice)} />
          <AnalysisCard run={run} />
          <Endpoints run={run} project={currentProject} onStop={onStop} stopping={stopping} />
          <PatchList patches={run.patches} />
          <LogConsole logs={run.logs} running={run.status === 'running'} height={320} active={panel === 'details'} />
        </div>
        <div className="drawer-section" hidden={panel !== 'projects'}>
          <ProjectsPanel projects={projects} currentId={run.id} onWatch={watchProject} onStop={onStop} stopping={stopping} autoFollow={autoFollow} setAutoFollow={setAutoFollow} />
        </div>
        <div className="drawer-section" hidden={panel !== 'nodes'}>
          {fleet ? <FleetPanel fleet={fleet} /> : <p>노드 풀 데이터를 불러오지 못했습니다.</p>}
        </div>
        <div className="drawer-section settings-section" hidden={panel !== 'settings'}>
          <h3>API 토큰</h3>
          <p>인증이 필요한 서버에 연결할 때 설정하세요.</p>
          <label htmlFor="api-token">API 토큰</label>
          <input id="api-token" type="password" autoComplete="off" value={token} onChange={(e) => saveToken(e.target.value)} placeholder="API 토큰 입력" />
          <p>{token ? '토큰이 설정되어 있습니다.' : '토큰이 설정되지 않았습니다.'}</p>
        </div>
      </DetailsDrawer>
    </div>
  );
}
