import React from 'react';
import { CheckCircle2, Cpu, Globe, Loader2, Rocket, Square, Wrench, XCircle } from 'lucide-react';
import { TARGET_META, stepStatus } from '../lib/run';
import { Badge, Dot } from './ui';
import { fmtDur } from '../lib/style';

const STEPS = [
  { id: 'analyze', title: '코드 분석', desc: 'Claude 검사관이 타깃·Dockerfile 결정', icon: Cpu },
  { id: 'deploy', title: '빌드 & 배포', desc: '블루그린 · 노드 풀 · 헬스체크', icon: Rocket },
  { id: 'heal', title: 'AI 자가치유', desc: 'stderr 분류 → 패치 → 재배포', icon: Wrench },
  { id: 'live', title: '공개 URL', desc: '누구나 접속 가능한 주소', icon: Globe },
];

const STYLE = {
  idle: 'border-white/5 bg-white/[0.02] text-slate-500',
  active: 'border-violet-500/50 bg-violet-500/10 text-violet-100 shadow-lg shadow-violet-900/30',
  done: 'border-emerald-500/30 bg-emerald-500/5 text-slate-200',
  skipped: 'border-white/5 bg-white/[0.02] text-slate-500',
  failed: 'border-rose-500/40 bg-rose-500/10 text-rose-100',
  partial: 'border-amber-500/40 bg-amber-500/10 text-amber-100',
  cancelled: 'border-white/5 bg-white/[0.02] text-slate-500',
};

const PHASE = {
  pending: { tone: 'slate', label: '대기' },
  deploying: { tone: 'violet', label: '배포 중', pulse: true },
  healing: { tone: 'amber', label: '치유 중', pulse: true },
  done: { tone: 'emerald', label: '완료' },
  failed: { tone: 'rose', label: '실패' },
  cancelled: { tone: 'slate', label: '중단' },
};

export default function Stepper({ run, now, onCancel, cancelling }) {
  const st = stepStatus(run);
  const elapsed = run.status === 'idle' ? null : Math.max(0, (run.finishedAt || now) - run.startedAt);
  return (
    <div className="rounded-2xl border border-white/5 bg-slate-900/60 p-4 shadow-xl shadow-black/20">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2 px-1">
        <div className="flex items-center gap-2 text-sm">
          {run.status === 'idle' ? <span className="text-slate-500">대기 중</span> : (
            <>
              {run.status === 'running' ? <Loader2 className="h-4 w-4 animate-spin text-violet-300" /> : run.status === 'completed' ? <CheckCircle2 className="h-4 w-4 text-emerald-400" /> : <XCircle className="h-4 w-4 text-rose-400" />}
              <span className="font-semibold text-slate-200">{{ running: '실행 중', completed: '모든 타깃 배포 완료', partial_failure: '일부 타깃 실패', failed: '실패', cancelled: '중단됨' }[run.status]}</span>
              <span className="font-mono text-xs text-slate-500">#{run.id}</span>
              {run.trigger === 'github-push' && <Badge tone="sky">GitHub push</Badge>}
            </>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {run.targets.map((t) => {
            const s = run.targetState[t] || { phase: 'pending' };
            const ph = PHASE[s.phase];
            return (
              <span key={t} className="flex items-center gap-1.5 text-xs text-slate-400">
                <Dot tone={ph.tone} pulse={ph.pulse} /> <span className="font-medium text-slate-300">{TARGET_META[t]?.label || t}</span> {ph.label}
                {s.attempts > 0 && <span className="text-amber-300">· 패치 {s.attempts}회</span>}
              </span>
            );
          })}
          {elapsed != null && <span className="font-mono text-xs tabular-nums text-slate-500">{fmtDur(elapsed)}</span>}
          {run.status === 'running' && run.id && (
            <button type="button" onClick={onCancel} disabled={cancelling}
              className="flex items-center gap-1 rounded-lg border border-rose-500/30 px-2 py-1 text-[11px] text-rose-300 hover:border-rose-400/60 hover:bg-rose-500/10 hover:text-rose-200 disabled:opacity-60">
              <Square className="h-3.5 w-3.5" /> {cancelling ? '중단 중…' : '중단'}
            </button>
          )}
        </div>
      </div>
      <ol className="grid grid-cols-2 gap-2 md:grid-cols-4">
        {STEPS.map((s, i) => {
          const state = st[s.id];
          const Icon = s.icon;
          return (
            <li key={s.id} className={`relative rounded-xl border px-3.5 py-3 transition-all duration-500 ${STYLE[state]}`}>
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-bold tracking-widest text-slate-500">STEP {i + 1}</span>
                {state === 'active' ? <Loader2 className="h-4 w-4 animate-spin text-violet-300" /> : state === 'done' ? <CheckCircle2 className="h-4 w-4 text-emerald-400" /> : state === 'failed' ? <XCircle className="h-4 w-4 text-rose-400" /> : <Icon className="h-4 w-4 text-slate-600" />}
              </div>
              <p className="mt-1.5 text-sm font-semibold">{s.title}</p>
              <p className="text-[11px] text-slate-500">{state === 'skipped' ? '오류 없음 · 건너뜀' : state === 'cancelled' ? '중단됨' : s.desc}</p>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
