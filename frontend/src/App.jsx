import React, { useEffect, useRef, useState } from 'react';
import {
  Cloud, Server, Play, CheckCircle2, AlertTriangle, Wrench, Terminal, ExternalLink,
  Cpu, FolderGit2, RefreshCw, Network, KeyRound, Activity,
} from 'lucide-react';
import { getDeployment, getFleet, getToken, setToken, startDeploy, subscribe } from './api';

// Tailwind only emits classes it can see literally, so every tone variant is spelled out here.
const TONES = {
  emerald: { chip: 'bg-emerald-950/50 border-emerald-500/60 text-emerald-300', card: 'bg-emerald-950/40 border-emerald-500/50 hover:bg-emerald-900/40', icon: 'text-emerald-400', active: 'bg-emerald-950/30 border-emerald-500 shadow-lg', passed: 'bg-slate-900 border-emerald-500/40', label: 'text-emerald-400' },
  indigo: { chip: 'bg-indigo-950/50 border-indigo-500/60 text-indigo-300', card: 'bg-indigo-950/40 border-indigo-500/50 hover:bg-indigo-900/40', icon: 'text-indigo-400', active: 'bg-indigo-950/30 border-indigo-500 shadow-lg', passed: 'bg-slate-900 border-indigo-500/40', label: 'text-indigo-400' },
  sky: { chip: 'bg-sky-950/50 border-sky-500/60 text-sky-300', card: 'bg-sky-950/40 border-sky-500/50 hover:bg-sky-900/40', icon: 'text-sky-400', active: 'bg-sky-950/30 border-sky-500 shadow-lg', passed: 'bg-slate-900 border-sky-500/40', label: 'text-sky-400' },
  red: { chip: '', card: '', icon: 'text-red-400', active: 'bg-red-950/30 border-red-500 shadow-lg', passed: 'bg-slate-900 border-red-500/40', label: 'text-red-400' },
  amber: { chip: '', card: '', icon: 'text-amber-400', active: 'bg-amber-950/30 border-amber-500 shadow-lg', passed: 'bg-slate-900 border-amber-500/40', label: 'text-amber-400' },
};
const TARGET_META = {
  local: { label: 'On-Premise (로컬 PC Docker)', icon: Server, tone: 'emerald' },
  cloudrun: { label: 'Google Cloud Run (서버리스)', icon: Cloud, tone: 'indigo' },
  node: { label: 'Node Pool (멀티 클라우드 VM)', icon: Network, tone: 'sky' },
};

const fmtTime = () => new Date().toLocaleTimeString();

export default function App() {
  const [source, setSource] = useState('sample-apps/broken');
  const [targets, setTargets] = useState({ local: true, cloudrun: true, node: false });
  const [token, setTokenState] = useState(getToken());
  const [job, setJob] = useState(null); // { id, status }
  const [step, setStep] = useState(0); // 0 idle, 1 analyze, 2 deploy, 3 heal, 4 done, -1 failed
  const [logs, setLogs] = useState([]);
  const [patches, setPatches] = useState([]); // PatchRecord + target
  const [endpoints, setEndpoints] = useState({}); // target -> url
  const [failures, setFailures] = useState({}); // target -> message
  const [fleet, setFleet] = useState(null);
  const closeRef = useRef(null);
  const finishedRef = useRef(new Set());
  const logEndRef = useRef(null);

  const addLog = (line, kind = 'info') => setLogs((prev) => [...prev, { t: fmtTime(), line, kind }]);
  useEffect(() => { logEndRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [logs]);

  // node pool panel: poll while visible
  useEffect(() => {
    let alive = true;
    const tick = async () => { try { const f = await getFleet(); if (alive) setFleet(f); } catch { /* backend down */ } };
    tick();
    const h = setInterval(tick, 15000);
    return () => { alive = false; clearInterval(h); };
  }, []);

  const selected = Object.keys(targets).filter((t) => targets[t]);
  const isDeploying = job && job.status === 'running';

  const finishIfDone = (id) => {
    if (finishedRef.current.size >= selected.length) {
      closeRef.current?.();
      getDeployment(id).then((d) => {
        setJob({ id, status: d.status });
        setStep(d.status === 'completed' || d.status === 'partial_failure' ? 4 : -1);
      }).catch(() => setJob({ id, status: 'done' }));
    }
  };

  const handleDeploy = async () => {
    if (!selected.length) return addLog('타깃을 하나 이상 선택하세요', 'error');
    setLogs([]); setPatches([]); setEndpoints({}); setFailures({}); setStep(1);
    finishedRef.current = new Set();
    closeRef.current?.();
    let id;
    try {
      id = await startDeploy(source, selected);
    } catch (e) {
      setStep(-1); return addLog(`POST /deploy 실패: ${e.message}`, 'error');
    }
    setJob({ id, status: 'running' });
    addLog(`deployment ${id} 시작 · source=${source} · targets=${selected.join(', ')}`);

    closeRef.current = subscribe(id, (type, p, raw) => {
      const stage = raw?.stage;
      if (type === 'stage') {
        if (stage === 'analyze') { setStep(1); addLog('🔍 AI 검사관이 저장소를 읽고 배포 타깃·Dockerfile을 결정합니다'); }
        if (stage === 'deploy') { setStep((s) => Math.max(s, 2)); addLog(`🐳 [${p.target}] 빌드 → 배포 → 헬스체크`); }
        if (stage === 'heal') { setStep(3); addLog(`🚨 [${p.target}] 배포 실패 · stderr 분석 중`, 'error'); if (p.stderr) addLog(p.stderr.split('\n').slice(-6).join('\n'), 'stderr'); }
        if (stage === 'redeploy') { addLog(`🔄 패치 #${p.attempt} 적용 후 재배포`, 'heal'); }
      } else if (type === 'log') {
        addLog(p.line || JSON.stringify(p));
      } else if (type === 'heal_diff') {
        setPatches((prev) => [...prev, p]);
        addLog(`🛠️ [Self-Healing #${p.attempt}] ${p.category} (${p.source}) — ${p.rationale}`, 'heal');
      } else if (type === 'done') {
        const url = p.url || p.final_url;
        if (p.target) { setEndpoints((prev) => ({ ...prev, [p.target]: url })); finishedRef.current.add(p.target); }
        addLog(`🎉 [${p.target}] 배포 완료 → ${url}${p.summary ? ' · ' + p.summary : ''}`, 'success');
        finishIfDone(id);
      } else if (type === 'error') {
        const msg = p.message || p.summary || JSON.stringify(p).slice(0, 300);
        if (p.target) { setFailures((prev) => ({ ...prev, [p.target]: msg })); finishedRef.current.add(p.target); addLog(`❌ [${p.target}] 실패: ${msg}`, 'error'); finishIfDone(id); }
        else { addLog(`❌ 파이프라인 오류: ${msg}`, 'error'); setStep(-1); setJob({ id, status: 'failed' }); closeRef.current?.(); }
      }
    }, () => { /* reconnect handled by EventSource; final state via finishIfDone */ });
  };

  const saveToken = (v) => { setToken(v); setTokenState(v); };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 p-8 font-sans">
      <header className="max-w-6xl mx-auto mb-8 flex items-center justify-between border-b border-slate-800 pb-5">
        <div className="flex items-center gap-3">
          <div className="bg-indigo-600 p-2.5 rounded-xl shadow-lg shadow-indigo-500/30"><Cloud className="w-8 h-8 text-white" /></div>
          <div>
            <h1 className="text-2xl font-bold bg-gradient-to-r from-white to-slate-400 bg-clip-text text-transparent">CloudMorph</h1>
            <p className="text-sm text-slate-400">One Action, Infinite Clouds — 분석 · 배포 · 자가치유 · 멀티 클라우드 노드 풀</p>
          </div>
        </div>
        <div className="flex items-center gap-2 text-xs">
          <KeyRound className="w-4 h-4 text-slate-500" />
          <input type="password" placeholder="API token (공개 터널 시)" value={token} onChange={(e) => saveToken(e.target.value)}
            className="bg-slate-900 border border-slate-800 rounded-lg px-3 py-1.5 w-48 focus:outline-none focus:border-indigo-500" />
        </div>
      </header>

      <main className="max-w-6xl mx-auto space-y-6">
        <section className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 items-end">
            <div>
              <label className="block text-xs font-medium text-slate-400 mb-2 flex items-center gap-1.5"><FolderGit2 className="w-4 h-4 text-indigo-400" /> 소스 (서버 경로 · ZIP · GitHub URL)</label>
              <input type="text" value={source} onChange={(e) => setSource(e.target.value)}
                className="w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-3 text-sm focus:outline-none focus:border-indigo-500" />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-400 mb-2 flex items-center gap-1.5"><Cpu className="w-4 h-4 text-indigo-400" /> 배포 타깃</label>
              <div className="flex gap-2 flex-wrap">
                {Object.entries(TARGET_META).map(([k, m]) => (
                  <button key={k} type="button" onClick={() => setTargets((t) => ({ ...t, [k]: !t[k] }))}
                    className={`px-3 py-2 rounded-xl border text-xs font-semibold transition ${targets[k] ? TONES[m.tone].chip : 'bg-slate-950 border-slate-800 text-slate-500'}`}>
                    {k}
                  </button>
                ))}
              </div>
            </div>
            <button onClick={handleDeploy} disabled={!!isDeploying}
              className="w-full bg-indigo-600 hover:bg-indigo-500 disabled:bg-slate-800 text-white font-semibold py-3 px-6 rounded-xl shadow-lg shadow-indigo-600/30 transition flex items-center justify-center gap-2 cursor-pointer">
              {isDeploying ? <><RefreshCw className="w-5 h-5 animate-spin" /> 파이프라인 실행 중… ({job.id})</> : <><Play className="w-5 h-5 fill-current" /> One Action Deploy</>}
            </button>
          </div>
        </section>

        <section className="grid grid-cols-1 md:grid-cols-4 gap-4">
          {[
            { n: 1, title: '코드 분석 & 환경 결정', desc: 'Claude 검사관이 저장소를 읽고 타깃·Dockerfile 결정', icon: Cpu, tone: 'indigo' },
            { n: 2, title: '빌드 & 배포', desc: '이미지 빌드, 블루그린/노드 배포, 헬스체크', icon: AlertTriangle, tone: 'red' },
            { n: 3, title: 'AI 자가치유', desc: 'stderr 분류 → Dockerfile 패치 → 재배포 (≤3회)', icon: Wrench, tone: 'amber' },
            { n: 4, title: '공개 URL 발급', desc: '터널 · run.app · 노드 풀 라우터 호스트', icon: CheckCircle2, tone: 'emerald' },
          ].map((c) => {
            const active = step === c.n, passed = step > c.n || step === 4;
            const Icon = c.icon;
            return (
              <div key={c.n} className={`p-5 rounded-2xl border transition-all ${active ? TONES[c.tone].active : passed ? TONES[c.tone].passed : 'bg-slate-900/40 border-slate-800 opacity-50'}`}>
                <div className="flex items-center justify-between mb-3"><span className={`text-xs font-bold ${TONES[c.tone].label}`}>STEP {c.n}</span><Icon className={`w-5 h-5 ${active ? `${TONES[c.tone].icon} animate-pulse` : 'text-slate-500'}`} /></div>
                <h3 className="font-bold text-base mb-1">{c.title}</h3><p className="text-xs text-slate-400">{c.desc}</p>
              </div>
            );
          })}
        </section>

        <section className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-5 flex flex-col h-[28rem]">
            <div className="flex items-center gap-2 text-xs font-semibold text-slate-400 mb-3 border-b border-slate-800 pb-3"><Terminal className="w-4 h-4 text-indigo-400" /> 실시간 파이프라인 이벤트 (SSE)</div>
            <div className="flex-1 bg-slate-950 rounded-xl p-4 font-mono text-xs overflow-y-auto space-y-1.5">
              {logs.length === 0 ? <p className="text-slate-600">One Action Deploy를 누르면 백엔드의 분석·배포·자가치유 이벤트가 여기에 흐릅니다.</p> :
                logs.map((l, i) => (
                  <div key={i} className={{ error: 'text-red-400', stderr: 'text-red-300/80 whitespace-pre-wrap pl-4', heal: 'text-amber-300', success: 'text-emerald-400 font-bold', info: 'text-slate-300' }[l.kind]}>
                    <span className="text-slate-600">[{l.t}]</span> {l.line}
                  </div>
                ))}
              <div ref={logEndRef} />
            </div>
          </div>

          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-5 flex flex-col h-[28rem]">
            <h3 className="text-sm font-bold text-slate-200 mb-3 flex items-center gap-2"><Wrench className="w-4 h-4 text-amber-400" /> 자가치유 패치 (Dockerfile diff)</h3>
            <div className="flex-1 overflow-y-auto space-y-3">
              {patches.length === 0 ? (
                <div className="bg-slate-950 border border-slate-800 rounded-xl p-8 text-center text-xs text-slate-600">배포가 실패하면 healer가 적용한 패치의 diff가 여기에 쌓입니다.</div>
              ) : patches.map((p, i) => (
                <div key={i} className="bg-slate-950 border border-slate-800 rounded-xl p-3 font-mono text-[11px]">
                  <div className="text-slate-400 mb-1">#{p.attempt} <span className="text-amber-300">{p.category}</span> · {p.source} · {p.rationale}</div>
                  <pre className="whitespace-pre-wrap leading-4">{(p.diff || '').split('\n').map((ln, j) => (
                    <div key={j} className={ln.startsWith('+') && !ln.startsWith('+++') ? 'text-emerald-400' : ln.startsWith('-') && !ln.startsWith('---') ? 'text-red-400' : ln.startsWith('@@') ? 'text-sky-400' : 'text-slate-500'}>{ln}</div>
                  ))}</pre>
                </div>
              ))}
            </div>
            <div className="mt-3">
              <h3 className="text-sm font-bold text-slate-200 mb-2">🌐 배포된 엔드포인트</h3>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                {selected.map((t) => {
                  const m = TARGET_META[t]; const Icon = m.icon; const url = endpoints[t]; const fail = failures[t];
                  return (
                    <a key={t} href={url || '#'} target="_blank" rel="noreferrer"
                      className={`p-3 rounded-xl border flex items-center justify-between transition ${url ? TONES[m.tone].card : fail ? 'bg-red-950/30 border-red-500/40' : 'bg-slate-950 border-slate-800 opacity-50 pointer-events-none'}`}>
                      <div className="flex items-center gap-2 min-w-0"><Icon className={`w-4 h-4 ${TONES[m.tone].icon} shrink-0`} />
                        <div className="min-w-0"><div className="text-[11px] font-bold">{t}</div><div className="text-[10px] text-slate-400 truncate">{url || fail || m.label}</div></div></div>
                      <ExternalLink className="w-3.5 h-3.5 text-slate-500 shrink-0" />
                    </a>
                  );
                })}
              </div>
            </div>
          </div>
        </section>

        {fleet && (
          <section className="bg-slate-900 border border-slate-800 rounded-2xl p-5">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-bold text-slate-200 flex items-center gap-2"><Activity className="w-4 h-4 text-sky-400" /> 노드 풀 (멀티 클라우드) · 라우터 {fleet.router ? `${fleet.router.host}:${fleet.router.public_port}` : '없음'}</h3>
              <span className="text-[11px] text-slate-500">15초마다 갱신 · CPU 임계값 초과 시 다른 노드로 자동 복제</span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="space-y-2">
                {fleet.nodes.map((n) => (
                  <div key={n.node} className="bg-slate-950 border border-slate-800 rounded-xl p-3 text-xs flex items-center justify-between">
                    <div><span className="font-bold">{n.node}</span> <span className="text-slate-500">{n.provider} · {n.arch} · {n.host}</span></div>
                    <div className={`font-mono ${n.ok ? 'text-slate-300' : 'text-red-400'}`}>{n.ok ? `load ${n.load1}/${n.cpus} · ${n.containers} ctr · score ${n.score}` : `unreachable`}</div>
                  </div>
                ))}
              </div>
              <div className="space-y-2">
                {fleet.apps.length === 0 ? <div className="text-xs text-slate-600 p-3">노드 풀에 배포된 앱이 없습니다. 타깃 node를 선택해 배포하세요.</div> :
                  fleet.apps.map((a) => (
                    <div key={a.name} className="bg-slate-950 border border-slate-800 rounded-xl p-3 text-xs">
                      <div className="flex items-center justify-between"><a className="font-bold text-sky-300 hover:underline" href={a.url} target="_blank" rel="noreferrer">{a.name}</a><span className="text-slate-500">{a.replicas.length} replica</span></div>
                      <div className="mt-1 flex flex-wrap gap-1.5">{a.replicas.map((r) => <span key={r.upstream} className="px-2 py-0.5 rounded bg-slate-900 border border-slate-800 font-mono">{r.node} {r.cpu != null ? `${r.cpu}%` : ''}</span>)}</div>
                      {a.events.slice(-3).reverse().map((e, i) => <div key={i} className={`mt-1 ${e.type === 'scale_out' ? 'text-amber-300' : 'text-slate-500'}`}>{new Date(e.ts * 1000).toLocaleTimeString()} {e.type} {e.node || ''} {e.reason || ''}</div>)}
                    </div>
                  ))}
              </div>
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
