import React, { useState } from 'react';
import { Zap, Cloud, Cpu, ExternalLink, GitPullRequest, Globe, Network, Server, Wrench } from 'lucide-react';
import { TARGET_META, canStop } from '../lib/run';
import { Badge, Card, CopyButton, Dot, StopButton } from './ui';
import { TONE } from '../lib/style';
import { diffRows } from '../lib/diff';

const ICONS = { local: Server, cloudrun: Cloud, node: Network, function: Zap };

export function AnalysisCard({ run }) {
  const a = run.analysis;
  return (
    <Card title="AI 분석 결과" icon={Cpu}>
      {!a ? (
        <p className="text-xs text-slate-400">{run.status === 'running' ? 'Claude 검사관이 저장소를 읽는 중입니다…' : '배포를 시작하면 언어 · 프레임워크 · 권장 타깃 · 위험 요소가 표시됩니다.'}</p>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-1.5">
            <Badge tone="violet">권장 {a.target}</Badge>
            <Badge>{a.language}</Badge>
            {a.framework && a.framework !== 'None' && <Badge>{a.framework}</Badge>}
            <Badge>port {a.port}</Badge>
            {a.models.map((m) => <Badge key={m} tone="sky">{m}</Badge>)}
          </div>
          <ul className="max-h-48 space-y-2 overflow-y-auto pr-1">
            {run.notes.map((n, i) => (
              <li key={i} className="flex gap-2.5 text-[12px] leading-5 text-slate-300">
                <span className={`mt-[6px] h-2 w-2 shrink-0 rounded-full ${/위험|해결|실패|사라|불가|없어/.test(n) ? 'bg-[#FFE27A] shadow-[0_0_8px_rgba(255,226,122,0.6)]' : 'bg-slate-600'}`} />
                <span>{n}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

function DiffBlock({ diff }) {
  return (
    <div className="space-y-1.5">
      <p className="text-[11px] text-slate-400">− 수정 전 · + 수정 후</p>
      <pre className="max-h-56 max-w-full overflow-auto rounded-xl border border-[rgba(150,160,215,0.16)] bg-[#0b0d17]/85 p-3 font-mono text-[11px] leading-[1.2rem] text-slate-300 shadow-inner">
        {diffRows(diff || '').map(({ kind, text }, j) => (
          <div key={j} data-diff-kind={kind} className={kind === 'added' ? 'diff-added px-1 rounded-sm' : kind === 'removed' ? 'diff-removed px-1 rounded-sm' : kind === 'file' ? 'mt-2 mb-1 text-slate-100 font-semibold' : kind === 'hunk' ? 'text-sky-300 font-semibold' : 'text-slate-400'}>
            {text || ' '}
          </div>
        ))}
      </pre>
    </div>
  );
}

function afterAnswer(q) {
  const lead = { timeout: '답이 없어서 이번 배포는 멈췄어요. ', 'github-push': 'GitHub push 배포라 묻지 않고 PR로만 제안했어요. ', guard: '장애 대응 중인 배포라 묻지 않고 PR로만 제안했어요. ' }[q.reason] || '';
  const prFail = q.pr_error ? ` PR은 올리지 못했어요 (${q.pr_error}).` : '';
  if (q.choice === 'continue') return `고친 코드로 배포를 이어가요.${q.pr_url ? ' 저장소에도 남도록 PR을 머지해 주세요.' : ' 같은 수정을 저장소에도 반영해 주세요.'}${prFail}`;
  if (q.choice === 'pr_only') return `${lead}PR을 올렸어요. 머지한 뒤 다시 배포해 주세요. 저장소의 GitHub webhook이 여기로 연결돼 있으면 머지하는 순간 자동으로 다시 배포돼요.${prFail}`;
  return `${lead}코드를 고치지 않았고, 이번 배포는 멈췄어요. 위 diff가 맞다면 직접 반영한 뒤 다시 배포해 주세요.`;
}

/** The healer wants to change the app's code: show the diff and ask *before* any PR is opened. */
export function QuestionCard({ question: q, onAnswer }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  if (!q) return null;
  const open = q.ask && !q.choice;
  const answer = async (choice) => {
    setBusy(true);
    setErr(null);
    try {
      await onAnswer(q.question_id, choice);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  const done = { continue: ['emerald', 'PR · 계속'], pr_only: ['sky', 'PR만'], stop: ['slate', '그만둠'] }[q.choice];
  return (
    <Card title={q.pr_url ? 'PR을 올렸어요' : 'AI가 코드를 고치려고 해요'} icon={GitPullRequest} className={open ? 'border-[#FFE27A]/50 shadow-[0_0_24px_rgba(255,226,122,0.08)]' : ''}
      right={open ? <Badge tone="amber">답변 대기</Badge> : done && <Badge tone={done[0]}>{done[1]}</Badge>}>
      <div className="space-y-3">
        <p className="text-[13px] leading-5 text-slate-300">{q.rationale}</p>
        {q.pr_url && (
          <a href={q.pr_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 font-mono text-[12px] text-sky-300 hover:text-sky-200">
            {q.pr_url} <ExternalLink className="h-3.5 w-3.5" />
          </a>
        )}
        <DiffBlock diff={q.diff} />
        {open && (
          <div className="space-y-2.5 rounded-xl border border-[rgba(255,226,122,0.35)] bg-[rgba(255,226,122,0.06)] p-3.5">
            <p className="text-sm font-semibold text-slate-100">코드를 바꾸기 전에 먼저 여쭤볼게요. 이렇게 고쳐도 될까요?</p>
            <p className="text-[12px] leading-5 text-slate-400">동의하시면 그때 PR을 올려요. 동의하기 전에는 저장소에 아무것도 올리지 않아요.</p>
            <div className="flex flex-wrap gap-2">
              <button type="button" disabled={busy} onClick={() => answer('stop')} className="rounded-full border border-[rgba(150,160,215,0.25)] bg-white/[0.04] px-3.5 py-1.5 text-[12.5px] font-medium text-slate-200 hover:bg-white/10 disabled:opacity-50 transition">
                고치지 않고 멈추기
              </button>
              {q.can_pr && (
                <button type="button" disabled={busy} onClick={() => answer('pr_only')} className="rounded-full border border-[rgba(150,160,215,0.25)] bg-white/[0.04] px-3.5 py-1.5 text-[12.5px] font-medium text-slate-200 hover:bg-white/10 disabled:opacity-50 transition">
                  PR만 올리고 멈추기
                </button>
              )}
              <button type="button" disabled={busy} onClick={() => answer('continue')} className="rounded-full bg-[#FFE27A] px-4 py-1.5 text-[12.5px] font-bold text-[#0b0d17] hover:bg-[#ffeb99] shadow-sm shadow-[#FFE27A]/20 disabled:opacity-50 transition">
                {q.can_pr ? 'PR 올리고 고친 채로 계속' : '고친 채로 계속'}
              </button>
            </div>
            <p className="text-[11px] text-slate-400">{Math.round((q.timeout_s || 300) / 60)}분 안에 답이 없으면 고치지 않고 멈춰요.</p>
            {err && <p className="text-xs text-rose-300">답변을 보내지 못했어요 · {err}</p>}
          </div>
        )}
        {q.choice && <p className="text-[13px] leading-5 text-slate-200">{afterAnswer(q)}</p>}
      </div>
    </Card>
  );
}

export function PatchList({ patches }) {
  return (
    <Card title="자가치유 패치" icon={Wrench} right={patches.length > 0 && <Badge tone="amber">{patches.length}건</Badge>}>
      {patches.length === 0 ? (
        <p className="text-xs text-slate-400">배포가 실패하면 healer가 고친 Dockerfile diff가 여기에 쌓입니다.</p>
      ) : (
        <div className="space-y-3">
          {patches.map((p, i) => (
            <div key={i} className="space-y-1.5">
              <div className="flex flex-wrap items-center gap-1.5 text-[12px]">
                <Badge tone="amber">#{p.attempt} {p.category}</Badge>
                {p.target && <Badge tone={TARGET_META[p.target]?.tone}>{p.target}</Badge>}
                <Badge>{p.source === 'rule' ? '규칙' : 'LLM'}</Badge>
                <span className="text-slate-300">{p.rationale}</span>
              </div>
              <DiffBlock diff={p.diff} />
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

export function SuggestionCard({ suggestion }) {
  if (!suggestion) return null;
  return (
    <Card title="소스 수정 제안 (적용 안 함)" icon={Wrench}>
      <p className="mb-2 text-xs text-slate-400">{suggestion.path} · 제안을 검토한 뒤 저장소에 반영해 주세요.</p>
      <DiffBlock diff={suggestion.diff} />
    </Card>
  );
}

export function Endpoints({ run, project, onStop, stopping }) {
  const targets = run.targets.length ? run.targets : [];
  const right = canStop(project)
    ? <StopButton onClick={() => onStop(project.name)} busy={stopping === project.name} />
    : project?.last_status === 'stopped' && <Badge>중지됨</Badge>;
  return (
    <Card title="공개 엔드포인트" icon={Globe} right={right}>
      {targets.length === 0 ? (
        <p className="text-xs text-slate-400">배포가 끝나면 타깃별 공개 URL이 표시됩니다.</p>
      ) : (
        <div className="space-y-2">
          {targets.map((t) => {
            const s = run.targetState[t] || {};
            const m = TARGET_META[t];
            const Icon = ICONS[t];
            // project.live is what the backend can still take down; a done target missing from it was stopped
            const down = s.phase === 'done' && !!project?.live && project.last_status !== 'running' && !project.live[t];
            const ok = s.phase === 'done' && s.url && !down;
            return (
              <div key={t} className={`flex items-center gap-3 rounded-[14px] border px-3.5 py-2.5 transition ${ok ? `${TONE[m.tone].border} bg-white/[0.04] shadow-sm` : s.phase === 'failed' ? 'border-rose-500/35 bg-rose-500/8' : 'border-[rgba(150,160,215,0.12)] bg-black/20'}`}>
                <span className={`grid h-8 w-8 shrink-0 place-items-center rounded-lg ring-1 ring-inset ring-white/10 ${TONE[m.tone].soft}`}><Icon className="h-4 w-4" /></span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 text-[13px] font-semibold text-slate-100">
                    {m.label}
                    {s.healed && <Badge tone="amber">자가치유됨</Badge>}
                  </div>
                  <div className={`truncate font-mono text-[11.5px] ${ok ? 'text-slate-200' : s.phase === 'failed' ? 'text-rose-300' : 'text-slate-500'}`}>
                    {ok ? s.url : down ? '중지됨' : s.phase === 'cancelled' ? '중단됨' : s.phase === 'failed' ? s.message : s.phase === 'pending' ? '대기' : <span className="flex items-center gap-1.5"><Dot tone="violet" pulse /> 진행 중</span>}
                  </div>
                </div>
                {ok && (
                  <>
                    <CopyButton text={s.url} />
                    <a href={s.url} target="_blank" rel="noreferrer" className="rounded-md p-1 text-slate-400 hover:bg-white/10 hover:text-white transition" title="열기"><ExternalLink className="h-4 w-4" /></a>
                  </>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}
