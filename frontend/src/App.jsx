import React, { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { answerQuestion, cancelDeploy, getAgents, getDeployment, getFleet, getHealth, getMe, getProjects, getToken, logOut, setToken, startDeploy, stopProject, submitEnv, subscribe } from './api';
import AuthScreen from './components/AuthScreen';
import { applyEvent, emptyRun } from './lib/run';
import DeployForm from './components/DeployForm';
import EnvRequestCard from './components/EnvRequestCard';
import { AnalysisCard, Endpoints, PatchList, QuestionCard, SuggestionCard } from './components/ResultPanels';
import AppsBoard from './components/AppsBoard';
import { DashboardHeader, RunHUD, ActivityDock } from './components/DashboardChrome';
import DetailsModal from './components/DetailsModal';
import { AgentPanel } from './components/OwnerPanels';
import { pendingInput } from './lib/dashboard';
import SceneBoundary from './components/3d/SceneBoundary';
import { shouldCelebrate } from './lib/victory';
import { EXPIRED_LOGS, canOpenLogs, canRedeploy, redeployRequest } from './lib/operations';
import { DEMO_RUN_ID, buildDemoFleet, buildDemoHandoffProject, buildDemoProjects } from './lib/demoData';

// three / fiber / drei live in lazy chunks so the 2D dashboard paints first
const PipelineJourney = lazy(() => import('./components/3d/PipelineJourney'));
const InstancedFleetGrid = lazy(() => import('./components/3d/InstancedFleetGrid'));
const VictoryCelebration = lazy(() => import('./components/3d/VictoryCelebration'));

const FLEET_DEMO_MAX = 500;
const FINISHED_RUN = new Set(['completed', 'partial_failure', 'failed', 'cancelled']);
const HIGHLIGHT_MS = 4000;

function parseDemoMode(search) {
  const params = new URLSearchParams(search);
  if (params.get('demo') === '1' || params.get('preview') === '1' || params.get('verify') === '1') return true;
  try {
    return localStorage.getItem('cloudmorph_demo') === '1';
  } catch {
    return false;
  }
}

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
  const [projectsState, setProjectsState] = useState({ status: 'loading', updatedAt: null });
  const projectsRequest = useRef(0);
  const mounted = useRef(true);
  const [fleet, setFleet] = useState(null);
  const [healthy, setHealthy] = useState(null);
  const [agents, setAgents] = useState([]); // the caller's on-premises agents ("내 PC" target)
  const [token, setTokenState] = useState(getToken());
  const [me, setMe] = useState(null);
  const [meChecked, setMeChecked] = useState(false); // false until the first /projects/me answer
  const [activeScene, setActiveScene] = useState('journey');
  const [panel, setPanel] = useState(null);
  const [fullscreen, setFullscreen] = useState(false);
  const shell = useRef(null);
  const [autoFollow, setAutoFollow] = useState(true);
  const [notice, setNotice] = useState(null);
  const [stopping, setStopping] = useState(() => new Set());
  const stopLocks = useRef(new Set());
  const launchLock = useRef(null);
  const tracking = useRef({ id: null, status: 'idle', name: null });
  const noticeTimer = useRef(null);
  const [cancelling, setCancelling] = useState(null);
  const [starting, setStarting] = useState(false);
  const [redeploying, setRedeploying] = useState(null);
  const [highlight, setHighlight] = useState(null);
  const [lookingUpLogs, setLookingUpLogs] = useState(false);
  const lookupLock = useRef(null);
  const lookupVersion = useRef(0);
  const closeRef = useRef(null);
  const seenDeployments = useRef(null);
  const [now, setNow] = useState(() => Date.now());
  const [dismissedVictoryIds, setDismissedVictoryIds] = useState(() => new Set());
  const [fleetDemo] = useState(() => parseFleetDemo(window.location.search));
  const [demoMode, setDemoMode] = useState(() => parseDemoMode(window.location.search));
  const [demoScenario, setDemoScenario] = useState('all');
  const [demoProjects, setDemoProjects] = useState(() => buildDemoProjects());
  const [demoFleetData] = useState(() => buildDemoFleet());
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

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; closeRef.current?.(); clearTimeout(noticeTimer.current); };
  }, []);
  useEffect(() => { tracking.current = { id: run.id, status: run.status, name: run.name }; }, [run.id, run.status, run.name]);

  const flash = useCallback((text) => {
    clearTimeout(noticeTimer.current);
    setNotice(text);
    noticeTimer.current = setTimeout(() => setNotice(null), 6000);
  }, []);

  const onToggleDemo = useCallback(() => {
    setDemoMode((prev) => {
      const next = !prev;
      try {
        if (next) localStorage.setItem('cloudmorph_demo', '1');
        else localStorage.removeItem('cloudmorph_demo');
      } catch {}
      if (next) {
        flash('시각 검증(데모) 모드가 켜졌습니다. 백엔드 없이 7가지 상태를 직접 확인해 보세요.');
        setActiveScene('apps');
      } else {
        flash('시각 검증 모드가 꺼졌습니다. 실제 백엔드 데이터로 돌아갑니다.');
      }
      return next;
    });
  }, [flash]);

  const onSelectDemoScenario = useCallback((s) => {
    setDemoScenario(s);
    if (s === 'handoff') {
      const handoffProj = buildDemoHandoffProject();
      setRun({
        ...emptyRun(DEMO_RUN_ID, {
          source: handoffProj.source,
          targets: handoffProj.targets,
          name: handoffProj.name,
          trigger: 'api',
        }),
        status: 'completed',
        name: 'handoff-app',
        urls: handoffProj.urls,
        targets: {
          local: { ok: true, url: handoffProj.urls.local },
          cloudrun: { ok: true, url: handoffProj.urls.cloudrun },
        },
        logs: [
          { id: 1, at: new Date(), kind: 'stdout', stage: 'completed', text: 'Cloud Run service ready: https://handoff-an.a.run.app' },
          { id: 2, at: new Date(), kind: 'stdout', stage: 'completed', text: 'Local Docker container running on port 8080' },
        ],
      });
      setActiveScene('journey');
    } else if (tracking.current.id === DEMO_RUN_ID) {
      setRun(emptyRun());
    }
  }, []);

  const effectiveProjectsState = useMemo(() => {
    if (!demoMode) return projectsState;
    if (demoScenario === 'error') {
      return { status: 'error', updatedAt: null };
    }
    return { status: 'ready', updatedAt: projectsState?.updatedAt ?? 0 };
  }, [demoMode, demoScenario, projectsState]);

  const effectiveFleet = useMemo(() => {
    if (!demoMode) return fleet;
    if (demoScenario === 'no-fleet') return null;
    return demoFleetData;
  }, [demoMode, demoScenario, fleet, demoFleetData]);

  const effectiveProjects = useMemo(() => {
    if (!demoMode) return projects;
    if (demoScenario === 'empty') return [];
    if (demoScenario === 'handoff') {
      const handoffProj = buildDemoHandoffProject();
      return [handoffProj, ...demoProjects.filter((p) => p.name !== handoffProj.name)];
    }
    return demoProjects;
  }, [demoMode, demoScenario, projects, demoProjects]);

  const invalidateLookup = useCallback(() => {
    lookupVersion.current += 1;
    lookupLock.current = null;
    setLookingUpLogs(false);
  }, []);
  const refreshProjects = useCallback(async () => {
    const version = ++projectsRequest.current;
    const p = await getProjects().catch(() => null);
    if (!mounted.current || version !== projectsRequest.current) return null;
    if (Array.isArray(p)) {
      setProjects(p);
      setProjectsState({ status: 'ready', updatedAt: Date.now() });
      return p;
    }
    setProjectsState((previous) => ({ ...previous, status: 'error' }));
    return null;
  }, []);

  // a fresh object per handoff click, so a repeat click restarts the timer
  useEffect(() => {
    if (!highlight) return undefined;
    const h = setTimeout(() => setHighlight(null), HIGHLIGHT_MS);
    return () => clearTimeout(h);
  }, [highlight]);

  // re-render once a second while running so elapsed time moves
  useEffect(() => {
    if (run.status !== 'running') return undefined;
    const h = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(h);
  }, [run.status]);

  const attach = useCallback((id, meta) => {
    invalidateLookup();
    tracking.current = { id, status: 'running', name: meta.name };
    closeRef.current?.();
    setActiveScene('journey');
    setHighlight(null);
    setPanel(null);
    setCancelling(null);
    setRun(emptyRun(id, meta));
    const version = lookupVersion.current;
    let checking = false;
    closeRef.current = subscribe(id, (type, p, raw) => setRun((r) => (r.id === id ? applyEvent(r, type, p, raw) : r)), async () => {
      if (checking) return;
      checking = true;
      try { await getDeployment(id); }
      catch (e) {
        if (e.message === '404' && version === lookupVersion.current && tracking.current.id === id) {
          closeRef.current?.();
          tracking.current = { id: null, status: 'idle', name: null };
          setRun(emptyRun()); setPanel(null); setActiveScene('apps'); flash(EXPIRED_LOGS);
        }
      } finally { checking = false; }
    });
  }, [invalidateLookup, flash]);

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
    let fetching = false;
    const poll = async () => {
      if (fetching) return;
      fetching = true;
      const [h, p, a] = await Promise.all([getHealth(), refreshProjects(), getAgents().catch(() => [])]);
      fetching = false;
      if (!alive) return;
      setHealthy(h);
      setAgents(a);
      if (p) {
        // auto-follow deployments started by a GitHub push
        const prev = seenDeployments.current;
        const canFollow = tracking.current.status !== 'running' && !launchLock.current;
        if (!prev || canFollow) seenDeployments.current = Object.fromEntries(p.map((x) => [x.name, x.last_deployment_id]));
        if (prev && autoFollow && canFollow) {
          const fresh = p.find((x) => x.trigger === 'github-push' && x.last_status === 'running' && prev[x.name] !== x.last_deployment_id);
          if (fresh) {
            flash(`GitHub push 감지 · ${fresh.name}@${fresh.ref} 배포를 따라갑니다`);
            attach(fresh.last_deployment_id, { source: fresh.source, targets: fresh.targets, name: fresh.name, trigger: 'github-push' });
          }
        }
      }
    };
    poll();
    const h = setInterval(poll, 4000);
    return () => { alive = false; clearInterval(h); };
  }, [autoFollow, attach, flash, refreshProjects]);

  useEffect(() => {
    let alive = true;
    const poll = async () => { const f = await getFleet().catch(() => null); if (alive) setFleet(f); };
    poll();
    const h = setInterval(poll, 15000);
    return () => { alive = false; clearInterval(h); };
  }, []);

  // whose dashboard this is: projects, deployments and CD links are per user
  useEffect(() => {
    let alive = true;
    const done = (m) => { if (alive) { setMe(m); setMeChecked(true); } };
    getMe().then(done).catch(() => done(null));
    return () => { alive = false; };
  }, [token]);

  const onDeploy = async () => {
    const name = form.name.trim();
    if (launchLock.current || tracking.current.status === 'running' || stopLocks.current.has(name)) return;
    launchLock.current = { name };
    invalidateLookup();
    const targets = Object.keys(form.targets).filter((t) => form.targets[t]);
    // lock the button before the request returns, otherwise a double click starts two deployments
    setStarting(true);
    try {
      const id = await startDeploy({ source: form.source.trim(), targets, name: form.name.trim() || undefined, ref: form.ref.trim() || undefined });
      attach(id, { source: form.source, targets, name: form.name || null, trigger: 'api' });
    } catch (e) {
      setRun({ ...emptyRun(), status: 'failed', error: e.message, logs: [{ id: 0, at: new Date(), kind: 'error', stage: 'failed', text: `배포 요청 실패: ${e.message}` }] });
    } finally {
      launchLock.current = null;
      setStarting(false);
    }
  };

  // StopButton already asked for confirmation
  const onStop = async (name) => {
    if (stopLocks.current.has(name) || launchLock.current?.name === name || (tracking.current.status === 'running' && tracking.current.name === name)) return;
    if (demoMode) {
      stopLocks.current.add(name);
      setStopping(new Set(stopLocks.current));
      setTimeout(() => {
        setDemoProjects((list) =>
          list.map((p) =>
            p.name === name
              ? { ...p, last_status: 'stopped', urls: {}, live: {} }
              : p
          )
        );
        stopLocks.current.delete(name);
        setStopping(new Set(stopLocks.current));
        flash(`${name} 중지됨`);
      }, 300);
      return;
    }
    stopLocks.current.add(name);
    setStopping(new Set(stopLocks.current));
    try {
      const r = await stopProject(name);
      const failed = Object.entries(r.failed);
      flash(failed.length ? `${name} 일부 중지 실패 · ${failed.map(([t, e]) => `${t}: ${e}`).join(' · ')}` : `${name} 중지됨`);
    } catch (e) {
      flash(`${name} 중지 실패 · ${e.message}`);
    } finally {
      await refreshProjects();
      stopLocks.current.delete(name);
      setStopping(new Set(stopLocks.current));
      getFleet().then(setFleet).catch(() => {});
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
  const currentProject = (effectiveProjects || projects)?.find((p) => p.last_deployment_id === run.id) || null;

  const runRunning = run.status === 'running';

  // The pipeline log is docked under the journey scene, so showing it needs no modal.
  const showLogs = () => setActiveScene('journey');
  const onOpenLogs = async (project) => {
    const current = tracking.current;
    if (!canOpenLogs(project, { runId: current.id, runRunning: current.status === 'running', launching: !!launchLock.current, lookingUpLogs: lookupLock.current !== null }).ok) return;
    if (demoMode) {
      if (project.name === 'broken-app' || project.last_status === 'failed') {
        flash(EXPIRED_LOGS);
        return;
      }
      setRun({
        ...emptyRun(project.last_deployment_id, {
          source: project.source,
          targets: project.targets || [],
          name: project.name,
          trigger: project.trigger,
        }),
        status: project.last_status,
        name: project.name,
        logs: [
          { id: 1, at: new Date(), kind: 'stdout', stage: 'analyze', text: `소스 분석 완료: ${project.source}` },
          { id: 2, at: new Date(), kind: 'stdout', stage: 'deploy', text: `타깃 배포: ${(project.targets || []).join(', ')}` },
          { id: 3, at: new Date(), kind: 'stdout', stage: 'live', text: `서비스 상태: ${project.last_status}` },
        ],
      });
      showLogs();
      return;
    }
    const id = project.last_deployment_id;
    if (id === current.id) { showLogs(); return; }
    const version = ++lookupVersion.current;
    lookupLock.current = version;
    setLookingUpLogs(true);
    try {
      await getDeployment(id);
      if (version !== lookupVersion.current || launchLock.current || tracking.current.status === 'running') return;
      attach(id, { source: project.source, targets: project.targets || [], name: project.name, trigger: project.trigger });
      showLogs();
    } catch (e) {
      if (version === lookupVersion.current) flash(e.message === '404' ? EXPIRED_LOGS : `로그를 불러오지 못했어요 · ${e.message}`);
    } finally {
      if (lookupLock.current === version) { lookupLock.current = null; setLookingUpLogs(false); }
    }
  };
  const onRedeploy = async (project) => {
    if (!canRedeploy(project, { runRunning: tracking.current.status === 'running', redeploying: !!launchLock.current, stopping: stopLocks.current.has(project.name) }).ok) return;
    if (demoMode) {
      setRedeploying(project.name);
      flash(`${project.name} 다시 배포 시작`);
      setTimeout(() => {
        setDemoProjects((list) =>
          list.map((p) =>
            p.name === project.name
              ? {
                  ...p,
                  last_status: 'completed',
                  updated_at: Date.now() / 1000,
                  urls: p.urls && Object.keys(p.urls).length > 0 ? p.urls : { local: `https://${p.name}.trycloudflare.com` },
                }
              : p
          )
        );
        setRedeploying(null);
        flash(`${project.name} 배포 완료`);
      }, 600);
      return;
    }
    const req = redeployRequest(project);
    launchLock.current = { name: project.name };
    invalidateLookup();
    setRedeploying(project.name);
    try {
      const id = await startDeploy(req);
      attach(id, { source: req.source, targets: req.targets, name: req.name, trigger: 'api' });
    } catch (e) {
      flash(`다시 배포 실패 · ${e.message}`);
    } finally {
      launchLock.current = null;
      setRedeploying(null);
    }
  };
  const saveToken = (v) => { setToken(v); setTokenState(v); };
  const onLogout = async () => { await logOut(); saveToken(''); };
  const celebrate = shouldCelebrate(run, dismissedVictoryIds);

  const dismissVictory = () => setDismissedVictoryIds((ids) => new Set(ids).add(run.id));
  const selectScene = (scene) => {
    if (celebrate) dismissVictory();
    if (scene !== 'apps') setHighlight(null);
    setActiveScene(scene);
  };
  const showInApps = () => {
    selectScene('apps');
    setHighlight({ name: currentProject.name });
  };
  const canHandoff = FINISHED_RUN.has(run.status) && !!currentProject;
  const showBoard = activeScene === 'apps' && !celebrate;
  const toggleFullscreen = async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await shell.current?.requestFullscreen?.();
    } catch {
      flash('전체화면을 열지 못했습니다. 현재 화면에서 계속 사용할 수 있습니다.');
    }
  };

  // no token, or one that maps to nobody (expired login): log in / sign up first
  if (!meChecked) return <div className="dashboard-shell" />; // don't flash the dashboard first
  if (!me) return <AuthScreen onAuthed={saveToken} healthy={healthy} />;

  return (
    <div ref={shell} className="dashboard-shell">
      <div className="dashboard-page" inert={panel ? true : undefined}>
        <DashboardHeader scene={activeScene} onScene={selectScene} healthy={healthy} onPanel={setPanel} fullscreen={fullscreen} onFullscreen={toggleFullscreen} demoMode={demoMode} onToggleDemo={onToggleDemo} user={me.user} admin={me.admin} onLogout={token ? onLogout : null} />
        <main className="dashboard-stage">
          <RunHUD run={run} now={now} notice={notice} onPanel={setPanel} onCancel={onCancel} cancelling={cancelling === run.id} celebrate={celebrate} onShowApp={canHandoff ? showInApps : null} />
          <div id="scene-view" className="scene-view" role="tabpanel" aria-labelledby={`scene-tab-${activeScene}`}>
            {showBoard ? (
              <AppsBoard
                projects={effectiveProjects}
                projectsState={effectiveProjectsState}
                onRefresh={demoMode ? () => setDemoProjects(buildDemoProjects()) : refreshProjects}
                fleet={effectiveFleet}
                currentRunId={run.id}
                currentRunName={run.name}
                launching={starting || !!redeploying}
                launchingName={redeploying || (starting ? form.name.trim() : null)}
                lookingUpLogs={lookingUpLogs}
                runRunning={runRunning}
                highlight={highlight}
                stopping={stopping}
                redeploying={redeploying}
                autoFollow={autoFollow}
                setAutoFollow={setAutoFollow}
                onOpenLogs={onOpenLogs}
                onRedeploy={onRedeploy}
                onStop={onStop}
                onNewDeploy={() => setPanel('deploy')}
                onShowNodes={() => selectScene('fleet')}
                demoMode={demoMode}
                scenario={demoScenario}
                setScenario={onSelectDemoScenario}
                onEnableDemo={() => { setDemoMode(true); flash('시각 검증(데모) 모드가 켜졌습니다.'); setActiveScene('apps'); }}
              />
            ) : <SceneBoundary key={celebrate ? `victory-${run.id}` : activeScene} fallback={<SceneMessage>3D 화면을 사용할 수 없습니다. 상태와 상세 패널에서 배포를 계속 확인할 수 있습니다.</SceneMessage>}>
              <Suspense fallback={<SceneMessage>3D 장면을 준비하고 있습니다…</SceneMessage>}>
                {celebrate ? <VictoryCelebration open run={run} presentation="stage" onClose={dismissVictory} />
                  : activeScene === 'journey' ? <PipelineJourney run={run} presentation="stage" />
                    : fleet ? <InstancedFleetGrid fleet={fleet} demoCount={fleetDemo} presentation="stage" />
                      : <SceneMessage>노드 풀 데이터를 불러오지 못했습니다. 연결 상태와 설정을 확인해 주세요.</SceneMessage>}
              </Suspense>
            </SceneBoundary>}
          </div>
          <ActivityDock run={run} project={currentProject} />
        </main>
      </div>
      <DetailsModal panel={panel} onPanel={setPanel} onClose={() => setPanel(null)}>
        <div className="modal-section" id="modal-section-deploy" role="region" aria-labelledby="modal-menu-deploy" hidden={panel !== 'deploy'}>
          <DeployForm form={form} setForm={setForm} onDeploy={onDeploy} busy={starting || runRunning || !!redeploying || stopping.has(form.name.trim())} fleetAvailable={!!fleet} agentOnline={agents.some((x) => x.online)} />
          <AgentPanel agents={agents} token={token} />
        </div>
        <div className="modal-section" id="modal-section-details" role="region" aria-labelledby="modal-menu-details" hidden={panel !== 'details'}>
          {run.envRequest && <EnvRequestCard key={`env-${run.id}`} request={run.envRequest} onSubmit={(values) => submitEnv(run.id, values)} />}
          <QuestionCard key={`question-${run.id}`} question={run.question} onAnswer={(qid, choice) => answerQuestion(run.id, qid, choice)} />
          {run.patches.length > 0 && <PatchList patches={run.patches} />}
          <SuggestionCard suggestion={run.suggestion} />
          <AnalysisCard run={run} />
          <Endpoints run={run} project={currentProject} onStop={onStop} stopping={stopping.has(currentProject?.name) ? currentProject?.name : null} stopDisabled={starting || !!redeploying || runRunning} />
        </div>
        <div className="modal-section settings-section" id="modal-section-settings" role="region" aria-labelledby="modal-menu-settings" hidden={panel !== 'settings'}>
          <h3>API 토큰</h3>
          <p>인증이 필요한 서버에 연결할 때 설정하세요.</p>
          <label htmlFor="api-token">API 토큰</label>
          <input id="api-token" type="password" autoComplete="off" value={token} onChange={(e) => saveToken(e.target.value)} placeholder="API 토큰 입력" />
          <p>{token ? '토큰이 설정되어 있습니다.' : '토큰이 설정되지 않았습니다.'}</p>
        </div>
      </DetailsModal>
    </div>
  );
}
