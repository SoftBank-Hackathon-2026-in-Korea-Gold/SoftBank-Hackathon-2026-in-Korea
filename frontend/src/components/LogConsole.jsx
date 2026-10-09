import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle, ArrowDownToLine, CheckCircle2, ChevronRight, Cpu, ExternalLink, GitBranch, Info, Rocket, Sparkles, Terminal, Wrench,
} from 'lucide-react';
import { TARGET_META } from '../lib/run';
import { Badge, Card, CopyButton } from './ui';
import { TONE, fmtTime } from '../lib/style';

const KIND = {
  stage: { icon: Rocket, tone: 'violet', label: 'STAGE' },
  analyze: { icon: Cpu, tone: 'violet', label: 'ANALYZE' },
  note: { icon: Info, tone: 'slate', label: 'NOTE' },
  push: { icon: GitBranch, tone: 'sky', label: 'PUSH' },
  heal: { icon: Wrench, tone: 'amber', label: 'HEAL' },
  success: { icon: CheckCircle2, tone: 'emerald', label: 'DONE' },
  error: { icon: AlertTriangle, tone: 'rose', label: 'ERROR' },
  info: { icon: Terminal, tone: 'slate', label: 'LOG' },
};

const FILTERS = [
  { id: 'all', label: '전체' },
  { id: 'key', label: '핵심만', test: (l) => l.kind !== 'note' && l.kind !== 'info' },
  { id: 'heal', label: '자가치유', test: (l) => l.kind === 'heal' || l.kind === 'error' },
  { id: 'error', label: '오류', test: (l) => l.kind === 'error' },
];

function Row({ log }) {
  const k = KIND[log.kind] || KIND.info;
  const Icon = k.icon;
  const tm = log.target ? TARGET_META[log.target] : null;
  const [open, setOpen] = useState(log.kind === 'error');
  const hasDetail = !!log.detail;
  return (
    <div className={`group border-l-2 py-1.5 pl-3 pr-2 ${TONE[k.tone].border} ${log.kind === 'note' ? 'opacity-70' : ''} hover:bg-white/[0.02]`}>
      <div className="flex items-start gap-2">
        <span className="mt-0.5 w-[64px] shrink-0 font-mono text-[11px] tabular-nums text-slate-500">{fmtTime(log.at)}</span>
        <Icon className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${TONE[k.tone].text}`} />
        <Badge tone={k.tone} className="mt-px w-[62px] shrink-0 justify-center font-mono">{k.label}</Badge>
        {tm && <Badge tone={tm.tone} className="mt-px shrink-0">{log.target}</Badge>}
        <div className="min-w-0 flex-1">
          <p className={`break-words text-[13px] leading-5 ${log.kind === 'error' ? 'text-rose-200' : log.kind === 'success' ? 'text-emerald-200' : log.kind === 'heal' ? 'text-amber-100' : 'text-slate-200'}`}>
            {hasDetail && (
              <button type="button" onClick={() => setOpen((o) => !o)} className="mr-1 inline-flex align-middle text-slate-500 hover:text-slate-200">
                <ChevronRight className={`h-3.5 w-3.5 transition-transform ${open ? 'rotate-90' : ''}`} />
              </button>
            )}
            {log.text}
            {log.url && (
              <a href={log.url} target="_blank" rel="noreferrer" className="ml-2 inline-flex items-center gap-1 font-mono text-[12px] text-emerald-300 underline-offset-2 hover:underline">
                {log.url} <ExternalLink className="h-3 w-3" />
              </a>
            )}
          </p>
          {hasDetail && open && (
            <div className="relative mt-1.5">
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-black/40 p-3 font-mono text-[11.5px] leading-[1.15rem] text-rose-100/80 ring-1 ring-rose-500/20">{log.detail}</pre>
              <CopyButton text={log.detail} className="absolute right-2 top-2" />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function LogConsole({ logs, running }) {
  const [filter, setFilter] = useState('key');
  const [target, setTarget] = useState('all');
  const [follow, setFollow] = useState(true);
  const endRef = useRef(null);
  const boxRef = useRef(null);

  const targets = useMemo(() => [...new Set(logs.map((l) => l.target).filter(Boolean))], [logs]);
  const shown = useMemo(() => {
    const f = FILTERS.find((x) => x.id === filter);
    return logs.filter((l) => (!f?.test || f.test(l)) && (target === 'all' || !l.target || l.target === target));
  }, [logs, filter, target]);

  useEffect(() => { if (follow) endRef.current?.scrollIntoView({ block: 'end' }); }, [shown.length, follow]);

  const onScroll = () => {
    const el = boxRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    if (atBottom !== follow) setFollow(atBottom);
  };

  return (
    <Card
      title="파이프라인 로그"
      icon={Terminal}
      className="min-w-0"
      bodyClass="p-0"
      right={
        <>
          <div className="flex rounded-lg bg-black/30 p-0.5 text-[11px]">
            {FILTERS.map((f) => (
              <button key={f.id} type="button" onClick={() => setFilter(f.id)} className={`rounded-md px-2 py-1 ${filter === f.id ? 'bg-white/10 text-white' : 'text-slate-400 hover:text-slate-200'}`}>{f.label}</button>
            ))}
          </div>
          {targets.length > 1 && (
            <select value={target} onChange={(e) => setTarget(e.target.value)} className="rounded-lg border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-slate-300">
              <option value="all">모든 타깃</option>
              {targets.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          )}
          <button type="button" title="맨 아래로 따라가기" onClick={() => { setFollow(true); endRef.current?.scrollIntoView({ block: 'end' }); }}
            className={`rounded-lg p-1.5 ${follow ? 'text-violet-300' : 'text-slate-500 hover:text-slate-200'}`}>
            <ArrowDownToLine className="h-4 w-4" />
          </button>
        </>
      }
    >
      <div ref={boxRef} onScroll={onScroll} className="h-[460px] overflow-y-auto px-2 py-2">
        {shown.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-center text-sm text-slate-500">
            <Sparkles className="h-6 w-6 text-slate-600" />
            <p>배포를 시작하면 분석 · 빌드 · 자가치유 · 공개 URL 발급 과정이 여기에 실시간으로 흐릅니다.</p>
            <p className="text-xs text-slate-600">GitHub에 push된 배포는 아래 프로젝트 목록에서 자동으로 따라옵니다.</p>
          </div>
        ) : (
          shown.map((l) => <Row key={l.id} log={l} />)
        )}
        {running && (
          <div className="flex items-center gap-2 py-2 pl-[86px] text-xs text-slate-500">
            <span className="flex gap-1">
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-violet-400 [animation-delay:-0.3s]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-violet-400 [animation-delay:-0.15s]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-violet-400" />
            </span>
            진행 중
          </div>
        )}
        <div ref={endRef} />
      </div>
      <footer className="flex items-center justify-between border-t border-white/5 px-4 py-2 text-[11px] text-slate-500">
        <span>{shown.length} / {logs.length} 줄</span>
        <span>{follow ? '자동 스크롤' : '스크롤 일시정지 · 위로 올려 둔 상태'}</span>
      </footer>
    </Card>
  );
}
