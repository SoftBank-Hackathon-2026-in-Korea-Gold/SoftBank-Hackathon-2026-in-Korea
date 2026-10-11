import React, { useState } from 'react';
import { ArrowRight, ChevronDown, ExternalLink, FolderGit2, Loader2, PartyPopper, ScrollText, XCircle } from 'lucide-react';
import AskCard from './AskCard';
import DecisionCard from './DecisionCard';
import Explain from './Explain';
import RunProgress from './RunProgress';
import { PLACES } from './copy';
import { explainFailure } from './failure';
import LogConsole from '../components/LogConsole';
import { PatchList, QuestionCard, SuggestionCard } from '../components/ResultPanels';
import { fmtDur } from '../lib/style';

const PRESETS = [
  { label: '방명록 앱 · DB 사용', value: 'sample-apps/guestbook' },
  { label: '고장 난 앱 · 자가치유', value: 'sample-apps/broken' },
  { label: 'FastAPI 앱', value: 'sample-apps/healthy-fastapi' },
  { label: '서버리스 함수', value: 'sample-apps/function-hello' },
  { label: 'GitHub 저장소 예시', value: 'https://github.com/Jeonsubb/cloudmorph-demo-guestbook' },
];
const PLACE_CHOICES = ['auto', 'local', 'cloudrun', 'node', 'function'];

function Hero({ form, setForm, onStart, busy, compact }) {
  const [open, setOpen] = useState(false);
  const start = (e) => {
    e.preventDefault();
    if (!busy && form.source.trim()) onStart();
  };
  return (
    <section className={compact ? '' : 'pt-6 sm:pt-10'}>
      {!compact && (
        <div className="mb-6">
          <h1 className="text-2xl font-bold leading-tight text-white sm:text-3xl">코드만 있으면 바로 배포해 드릴게요</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-400">
            GitHub 주소나 폴더 경로를 넣고 <b className="text-slate-200">배포하기</b>를 눌러 주세요. 어디에 올릴지는 CloudMorph가 코드를 읽고 1초 안에 정하고, 꼭 필요한 것만 물어볼게요.
          </p>
        </div>
      )}
      <form onSubmit={start} className="flex flex-col gap-2 sm:flex-row">
        <label className="flex min-w-0 flex-1 items-center gap-2 rounded-xl border border-white/10 bg-black/40 px-3.5 focus-within:border-violet-400/60">
          <FolderGit2 className="h-4 w-4 shrink-0 text-slate-500" />
          <span className="sr-only">배포할 코드</span>
          <input
            value={form.source}
            onChange={(e) => setForm((f) => ({ ...f, source: e.target.value }))}
            placeholder="https://github.com/내-아이디/내-저장소"
            className="min-w-0 flex-1 bg-transparent py-3 font-mono text-[13px] text-slate-100 placeholder:text-slate-600 focus:outline-none"
          />
        </label>
        <button type="submit" disabled={busy || !form.source.trim()}
          className="flex items-center justify-center gap-2 rounded-xl bg-white px-6 py-3 text-sm font-bold text-slate-900 transition hover:bg-slate-200 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400">
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <ArrowRight className="h-4 w-4" />} 배포하기
        </button>
      </form>
      <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
        {PRESETS.map((p) => (
          <button key={p.value} type="button" onClick={() => setForm((f) => ({ ...f, source: p.value }))}
            className={`rounded-full border px-2.5 py-1 text-[11px] transition ${form.source === p.value ? 'border-violet-400/50 bg-violet-500/10 text-violet-100' : 'border-white/10 text-slate-400 hover:border-white/25 hover:text-slate-200'}`}>
            {p.label}
          </button>
        ))}
        <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open} className="ml-auto flex items-center gap-1 text-[12px] text-slate-400 hover:text-slate-200">
          직접 고를래요 <ChevronDown className={`h-3.5 w-3.5 transition ${open ? 'rotate-180' : ''}`} />
        </button>
      </div>
      {!compact && !open && (
        <ol className="mt-10 grid gap-3 sm:grid-cols-3">
          {[
            ['1', '코드를 읽고 1초 안에 정해요', '어떤 서버가 맞는지 AI가 코드를 보고 골라요. 이유도 같이 보여 드려요.'],
            ['2', '필요한 것만 물어봐요', '비밀번호 같은 값이 필요하거나, 데이터가 사라질 수 있는 선택일 때만 여쭤봐요.'],
            ['3', '올린 뒤에도 지켜봐요', '문제가 생기면 Jev가 몇 초 안에 알아채고 고쳐요. 코드를 고쳐야 할 땐 먼저 여쭤보고, 동의하시면 PR로 올려요.'],
          ].map(([n, title, body]) => (
            <li key={n} className="rounded-2xl border border-white/5 bg-slate-900/50 p-4">
              <span className="grid h-6 w-6 place-items-center rounded-full bg-violet-500/20 text-[12px] font-bold text-violet-200">{n}</span>
              <p className="mt-2 text-sm font-semibold text-white">{title}</p>
              <p className="mt-1 text-[12px] leading-5 text-slate-400">{body}</p>
            </li>
          ))}
        </ol>
      )}
      {open && (
        <div className="mt-3 grid gap-3 rounded-xl border border-white/10 bg-black/20 p-4 sm:grid-cols-3">
          <label className="block text-[12px] text-slate-400">
            앱 이름
            <input value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, '') }))} placeholder="자동으로 정해요"
              className="mt-1 w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2 font-mono text-[13px] text-slate-100 placeholder:text-slate-600 focus:border-violet-400/60 focus:outline-none" />
            <span className="mt-1 block text-[11px] text-slate-500">같은 이름으로 다시 배포하면 같은 앱이 새 버전으로 바뀌어요.</span>
          </label>
          <label className="block text-[12px] text-slate-400">
            브랜치 (GitHub일 때)
            <input value={form.ref} onChange={(e) => setForm((f) => ({ ...f, ref: e.target.value }))} placeholder="기본 브랜치"
              className="mt-1 w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2 font-mono text-[13px] text-slate-100 placeholder:text-slate-600 focus:border-violet-400/60 focus:outline-none" />
          </label>
          <div className="text-[12px] text-slate-400">
            올릴 곳
            <div className="mt-1 flex flex-wrap gap-1.5">
              {PLACE_CHOICES.map((p) => (
                <button key={p} type="button" onClick={() => setForm((f) => ({ ...f, place: p }))} aria-pressed={form.place === p}
                  className={`rounded-lg border px-2.5 py-1.5 text-[12px] ${form.place === p ? 'border-violet-400/60 bg-violet-500/10 text-white' : 'border-white/10 text-slate-400 hover:text-slate-200'}`}>
                  {p === 'auto' ? '자동 (추천)' : PLACES[p].name}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function Result({ run, onOpenApps }) {
  if (!['completed', 'partial_failure', 'failed'].includes(run.status)) return null;
  const up = run.targets.map((t) => [t, run.targetState[t]]).filter(([, s]) => s?.phase === 'done' && s.url);
  const down = run.targets.map((t) => [t, run.targetState[t]]).filter(([, s]) => s?.phase === 'failed');
  return (
    <section className={`rounded-2xl border p-5 ${up.length ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-rose-500/30 bg-rose-500/5'}`}>
      {up.length > 0 ? (
        <>
          <h3 className="flex items-center gap-2 text-lg font-bold text-white"><PartyPopper className="h-5 w-5 text-emerald-300" /> 배포를 마쳤어요</h3>
          <div className="mt-3 space-y-2">
            {up.map(([t, s]) => (
              <a key={t} href={s.url} target="_blank" rel="noreferrer" className="flex items-center justify-between gap-3 rounded-xl border border-white/10 bg-black/30 px-4 py-3 hover:border-emerald-400/40">
                <span className="min-w-0">
                  <span className="block text-[12px] text-slate-400">{PLACES[t]?.name || t}{s.healed ? ' · AI가 고쳐서 띄웠어요' : ''}</span>
                  <span className="block truncate font-mono text-[13px] text-emerald-200">{s.url}</span>
                </span>
                <span className="flex shrink-0 items-center gap-1 text-[12px] font-semibold text-white">열어 보기 <ExternalLink className="h-3.5 w-3.5" /></span>
              </a>
            ))}
          </div>
          {run.name && <button type="button" onClick={() => onOpenApps(run.name)} className="mt-3 text-[12px] text-slate-300 underline-offset-4 hover:underline">서버 지도에서 보기 →</button>}
        </>
      ) : (
        <h3 className="flex items-center gap-2 text-lg font-bold text-white"><XCircle className="h-5 w-5 text-rose-300" /> 배포하지 못했어요</h3>
      )}
      {down.map(([t, s]) => {
        const why = explainFailure(s.message, s.stderr);
        return (
          <div key={t} className="mt-3 rounded-xl border border-rose-500/20 bg-black/20 p-3">
            <p className="text-[13px] font-semibold text-rose-100">{PLACES[t]?.name || t} · {why.title}</p>
            {why.lines.length > 0 && (
              <ul className="mt-1.5 list-disc space-y-1 pl-4 text-[12px] leading-5 text-rose-100/90">
                {why.lines.map((line) => <li key={line}>{line}</li>)}
              </ul>
            )}
          </div>
        );
      })}
      {run.error && !down.length && <p className="mt-2 whitespace-pre-wrap text-[12px] leading-5 text-rose-200">{run.error}</p>}
    </section>
  );
}

export default function DeployPage({ form, setForm, run, now, starting, cancelling, onStart, onAnswer, onAnswerQuestion, onCancel, onOpenApps }) {
  const [logs, setLogs] = useState(false);
  const active = run.status !== 'idle';
  const elapsed = active ? fmtDur((run.finishedAt || now) - run.startedAt) : null;
  const finalTarget = run.analysis?.target;
  return (
    <div className="space-y-5">
      <Hero form={form} setForm={setForm} onStart={onStart} busy={starting || run.status === 'running'} compact={active} />
      {active && (
        <>
          <RunProgress run={run} elapsed={elapsed} onCancel={onCancel} cancelling={cancelling} />
          <div className="grid gap-5 lg:grid-cols-2">
            <div className="space-y-5">
              <DecisionCard decision={run.decision} finalTarget={finalTarget} />
              {!run.decision && run.status === 'running' && (
                <section className="rounded-2xl border border-white/5 bg-slate-900/60 p-5 text-sm text-slate-400">
                  <Loader2 className="mr-2 inline h-4 w-4 animate-spin" /> 코드를 읽고 어디에 올릴지 정하고 있어요…
                </section>
              )}
              {run.analysis && (
                <p className="px-1 text-[12px] text-slate-500">
                  {run.analysis.language}{run.analysis.framework && run.analysis.framework !== 'None' ? ` · ${run.analysis.framework}` : ''} 앱이에요 · 포트 {run.analysis.port} ·{' '}
                  <Explain term="컨테이너" /> 하나로 만들어 올려요
                </p>
              )}
            </div>
            <div className="space-y-5">
              {run.envRequest && <AskCard key={run.id} request={run.envRequest} onSubmit={onAnswer} />}
              <QuestionCard question={run.question} onAnswer={onAnswerQuestion} />
              {run.patches.length > 0 && <PatchList patches={run.patches} />}
              <SuggestionCard suggestion={run.suggestion} />
              <Result run={run} onOpenApps={onOpenApps} />
            </div>
          </div>
          <div>
            <button type="button" onClick={() => setLogs((v) => !v)} aria-expanded={logs} className="flex items-center gap-1.5 text-[12px] text-slate-400 hover:text-slate-200">
              <ScrollText className="h-3.5 w-3.5" /> {logs ? '자세한 기록 접기' : '자세한 기록 보기'} ({run.logs.length})
            </button>
            {logs && <div className="mt-3"><LogConsole logs={run.logs} running={run.status === 'running'} /></div>}
          </div>
        </>
      )}
    </div>
  );
}
