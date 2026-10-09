import React from 'react';
import { Activity, Boxes, Eye, GitBranch, Layers, Radio, Server } from 'lucide-react';
import { Badge, Bar, Card, Dot } from './ui';
import { fmtAgo, fmtTime } from '../lib/style';

const STATUS = {
  running: { tone: 'violet', label: '배포 중', pulse: true },
  completed: { tone: 'emerald', label: '정상' },
  partial_failure: { tone: 'amber', label: '일부 실패' },
  failed: { tone: 'rose', label: '실패' },
};

export function ProjectsPanel({ projects, currentId, onWatch, autoFollow, setAutoFollow }) {
  return (
    <Card
      title="배포 중인 프로젝트"
      icon={Layers}
      right={
        <label className="flex cursor-pointer items-center gap-2 text-[11px] text-slate-400">
          <Radio className={`h-3.5 w-3.5 ${autoFollow ? 'text-sky-300' : ''}`} /> GitHub push 자동 추적
          <input type="checkbox" checked={autoFollow} onChange={(e) => setAutoFollow(e.target.checked)} className="accent-sky-400" />
        </label>
      }
      bodyClass="p-0"
    >
      {!projects || projects.length === 0 ? (
        <p className="p-5 text-xs text-slate-500">아직 배포된 프로젝트가 없습니다. 위에서 배포하거나, 웹훅이 연결된 저장소에 push하세요.</p>
      ) : (
        <div className="divide-y divide-white/5">
          {projects.map((p) => {
            const st = STATUS[p.last_status] || STATUS.running;
            const urls = Object.entries(p.urls || {}).filter(([, u]) => u);
            const watching = p.last_deployment_id === currentId;
            return (
              <div key={p.name} className={`flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3 ${watching ? 'bg-violet-500/5' : ''}`}>
                <div className="flex min-w-[220px] flex-1 items-center gap-3">
                  <Dot tone={st.tone} pulse={st.pulse} />
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="truncate font-mono text-[13px] font-semibold text-slate-100">{p.name}</span>
                      <Badge tone={st.tone}>{st.label}</Badge>
                      {p.trigger === 'github-push' ? <Badge tone="sky"><GitBranch className="h-3 w-3" />{p.ref || 'push'}</Badge> : <Badge>수동</Badge>}
                    </div>
                    <div className="truncate font-mono text-[11px] text-slate-500">{p.source}</div>
                  </div>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {urls.map(([t, u]) => (
                    <a key={t} href={u} target="_blank" rel="noreferrer" className="rounded-md bg-white/5 px-2 py-1 font-mono text-[11px] text-slate-300 ring-1 ring-inset ring-white/5 hover:bg-white/10 hover:text-white">{t} ↗</a>
                  ))}
                </div>
                <div className="flex items-center gap-3 text-[11px] text-slate-500">
                  <span>{(p.history || []).length}회 배포</span>
                  <span>{p.updated_at ? fmtAgo(p.updated_at) : ''}</span>
                  <button type="button" onClick={() => onWatch(p)} disabled={watching}
                    className="flex items-center gap-1 rounded-lg border border-white/10 px-2 py-1 text-slate-300 hover:border-white/20 hover:text-white disabled:border-violet-500/40 disabled:text-violet-300">
                    <Eye className="h-3.5 w-3.5" /> {watching ? '보는 중' : '로그 보기'}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

export function FleetPanel({ fleet }) {
  if (!fleet) return null;
  return (
    <Card
      title="노드 풀 · 멀티 클라우드"
      icon={Boxes}
      right={<span className="text-[11px] text-slate-500">라우터 {fleet.router ? `${fleet.router.host}:${fleet.router.public_port}` : '없음'} · 15초 갱신</span>}
    >
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="space-y-2">
          <h3 className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-500"><Server className="h-3.5 w-3.5" /> 노드</h3>
          {fleet.nodes.map((n) => {
            const load = n.ok ? Math.round((n.load1 / Math.max(n.cpus, 1)) * 100) : 0;
            const mem = n.ok && n.mem_total_mb ? Math.round((1 - n.mem_avail_mb / n.mem_total_mb) * 100) : 0;
            return (
              <div key={n.node} className="rounded-xl border border-white/5 bg-black/20 p-3">
                <div className="mb-2 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Dot tone={n.ok ? 'emerald' : 'rose'} />
                    <span className="font-mono text-[13px] font-semibold text-slate-200">{n.node}</span>
                    <Badge tone="sky">{n.provider}</Badge>
                    <Badge>{n.arch}</Badge>
                  </div>
                  <span className="font-mono text-[11px] text-slate-500">{n.host}</span>
                </div>
                {n.ok ? (
                  <div className="grid grid-cols-[52px_1fr_44px] items-center gap-x-2 gap-y-1.5 text-[11px] text-slate-400">
                    <span>CPU 부하</span><Bar value={load} /><span className="text-right font-mono tabular-nums">{load}%</span>
                    <span>메모리</span><Bar value={mem} tone="sky" /><span className="text-right font-mono tabular-nums">{mem}%</span>
                    <span>컨테이너</span><span className="text-slate-300">{n.containers}개</span><span className="text-right font-mono text-slate-500">{n.score}</span>
                  </div>
                ) : <p className="text-[11px] text-rose-300">접속 불가</p>}
              </div>
            );
          })}
        </div>
        <div className="space-y-2">
          <h3 className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-500"><Activity className="h-3.5 w-3.5" /> 앱 · 복제본</h3>
          {fleet.apps.length === 0 ? <p className="text-xs text-slate-500">노드 풀에 배포된 앱이 없습니다.</p> : fleet.apps.map((a) => (
            <div key={a.name} className="rounded-xl border border-white/5 bg-black/20 p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <a href={a.url} target="_blank" rel="noreferrer" className="truncate font-mono text-[13px] font-semibold text-sky-300 hover:underline">{a.name}</a>
                <Badge tone="violet">{a.replicas.length} replica</Badge>
              </div>
              <div className="space-y-1.5">
                {a.replicas.map((r) => (
                  <div key={r.upstream} className="grid grid-cols-[110px_1fr_44px] items-center gap-2 text-[11px] text-slate-400">
                    <span className="truncate font-mono">{r.node}</span><Bar value={r.cpu ?? 0} /><span className="text-right font-mono tabular-nums">{r.cpu != null ? `${Math.round(r.cpu)}%` : '–'}</span>
                  </div>
                ))}
              </div>
              {a.events.slice(-3).reverse().map((e, i) => (
                <div key={i} className={`mt-1.5 truncate text-[11px] ${e.type === 'scale_out' ? 'text-amber-300' : e.type === 'scale_in' ? 'text-sky-300' : 'text-slate-500'}`}>
                  {fmtTime(new Date(e.ts * 1000))} · {e.type} {e.node || ''} {e.reason ? `· ${e.reason}` : ''}
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
}
