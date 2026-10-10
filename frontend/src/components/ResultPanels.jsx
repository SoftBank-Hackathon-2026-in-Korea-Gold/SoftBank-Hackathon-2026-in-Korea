import React, { useState } from 'react';
import { Zap, Cloud, Cpu, ExternalLink, GitPullRequest, Globe, Network, Server, Wrench } from 'lucide-react';
import { TARGET_META } from '../lib/run';
import { Badge, Card, CopyButton, Dot } from './ui';
import { TONE } from '../lib/style';

const ICONS = { local: Server, cloudrun: Cloud, node: Network, function: Zap };

export function AnalysisCard({ run }) {
  const a = run.analysis;
  return (
    <Card title="AI 분석 결과" icon={Cpu}>
      {!a ? (
        <p className="text-xs text-slate-500">{run.status === 'running' ? 'Claude 검사관이 저장소를 읽는 중입니다…' : '배포를 시작하면 언어 · 프레임워크 · 권장 타깃 · 위험 요소가 표시됩니다.'}</p>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-1.5">
            <Badge tone="violet">권장 {a.target}</Badge>
            <Badge>{a.language}</Badge>
            {a.framework && a.framework !== 'None' && <Badge>{a.framework}</Badge>}
            <Badge>port {a.port}</Badge>
            {a.models.map((m) => <Badge key={m} tone="sky">{m}</Badge>)}
          </div>
          <ul className="max-h-48 space-y-1.5 overflow-y-auto pr-1">
            {run.notes.map((n, i) => (
              <li key={i} className="flex gap-2 text-[12px] leading-5 text-slate-300">
                <span className={`mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full ${/위험|해결|실패|사라|불가|없어/.test(n) ? 'bg-amber-400' : 'bg-slate-600'}`} />
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
    <pre className="max-h-56 max-w-full overflow-auto rounded-lg bg-black/40 p-2.5 font-mono text-[11px] leading-[1.1rem] ring-1 ring-white/5">
      {(diff || '').split('\n').filter((l) => !l.startsWith('---') && !l.startsWith('+++')).map((ln, j) => (
        <div key={j} className={ln.startsWith('+') ? 'bg-emerald-500/10 text-emerald-300' : ln.startsWith('-') ? 'bg-rose-500/10 text-rose-300' : ln.startsWith('@@') ? 'text-sky-400' : 'text-slate-500'}>
          {ln || ' '}
        </div>
      ))}
    </pre>
  );
}

function afterAnswer(q) {
  const lead = { timeout: '답이 없어서 이번 배포는 멈췄어요. ', 'github-push': 'GitHub push 배포라 묻지 않고 멈췄어요. ' }[q.reason] || '';
  if (q.choice === 'continue') return q.pr_url ? '고친 소스로 배포를 이어갑니다. 저장소에도 남도록 PR을 머지해 주세요.' : '고친 소스로 배포를 이어갑니다. 같은 수정을 저장소에도 반영해 주세요.';
  if (q.pr_url) return `${lead}PR을 머지한 뒤 다시 배포해 주세요. 저장소의 GitHub webhook이 여기로 연결돼 있으면 머지하는 순간 자동으로 다시 배포됩니다.`;
  return `${lead}위 diff를 저장소에 반영한 뒤 다시 배포해 주세요.`;
}

/** The healer fixed the app's source: PR link + diff, and "stop here, or continue with the fix?" */
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
  return (
    <Card title={q.pr_url ? 'PR을 올렸어요!' : '소스를 고쳤어요'} icon={GitPullRequest} className={open ? 'border-amber-500/40' : ''}
      right={open ? <Badge tone="amber">답변 대기</Badge> : q.choice && <Badge tone={q.choice === 'continue' ? 'emerald' : 'slate'}>{q.choice === 'continue' ? '계속' : '중단'}</Badge>}>
      <div className="space-y-3">
        <p className="text-[13px] leading-5 text-slate-300">{q.rationale}</p>
        {q.pr_url && (
          <a href={q.pr_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 font-mono text-[12px] text-sky-300 hover:text-sky-200">
            {q.pr_url} <ExternalLink className="h-3.5 w-3.5" />
          </a>
        )}
        {q.pr_error && <p className="text-xs text-amber-300">PR을 올리지 못했어요 · {q.pr_error}</p>}
        <DiffBlock diff={q.diff} />
        {open && (
          <div className="space-y-2 rounded-xl border border-amber-500/30 bg-amber-500/5 p-3">
            <p className="text-sm font-medium text-slate-100">이번 배포는 그만 둘까요? 아니면 바꾸고 계속 할까요?</p>
            <div className="flex flex-wrap gap-2">
              <button type="button" disabled={busy} onClick={() => answer('stop')} className="rounded-lg border border-white/10 bg-white/5 px-3 py-1.5 text-[13px] text-slate-200 hover:bg-white/10 disabled:opacity-50">
                그만 두기 (PR 머지 후 다시 배포)
              </button>
              <button type="button" disabled={busy} onClick={() => answer('continue')} className="rounded-lg bg-violet-600 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-violet-500 disabled:opacity-50">
                고친 채로 계속
              </button>
            </div>
            <p className="text-[11px] text-slate-500">{Math.round((q.timeout_s || 300) / 60)}분 안에 답이 없으면 이번 배포는 멈춥니다. PR은 그대로 남아요.</p>
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
        <p className="text-xs text-slate-500">배포가 실패하면 healer가 고친 Dockerfile diff가 여기에 쌓입니다.</p>
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

export function Endpoints({ run }) {
  const targets = run.targets.length ? run.targets : [];
  return (
    <Card title="공개 엔드포인트" icon={Globe}>
      {targets.length === 0 ? (
        <p className="text-xs text-slate-500">배포가 끝나면 타깃별 공개 URL이 표시됩니다.</p>
      ) : (
        <div className="space-y-2">
          {targets.map((t) => {
            const s = run.targetState[t] || {};
            const m = TARGET_META[t];
            const Icon = ICONS[t];
            const ok = s.phase === 'done' && s.url;
            return (
              <div key={t} className={`flex items-center gap-3 rounded-xl border px-3 py-2.5 ${ok ? `${TONE[m.tone].border} bg-white/[0.03]` : s.phase === 'failed' ? 'border-rose-500/30 bg-rose-500/5' : 'border-white/5'}`}>
                <span className={`grid h-8 w-8 shrink-0 place-items-center rounded-lg ring-1 ring-inset ring-white/5 ${TONE[m.tone].soft}`}><Icon className="h-4 w-4" /></span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 text-[13px] font-semibold text-slate-200">
                    {m.label}
                    {s.healed && <Badge tone="amber">자가치유됨</Badge>}
                  </div>
                  <div className={`truncate font-mono text-[11.5px] ${ok ? 'text-slate-300' : s.phase === 'failed' ? 'text-rose-300' : 'text-slate-500'}`}>
                    {ok ? s.url : s.phase === 'failed' ? s.message : s.phase === 'pending' ? '대기' : <span className="flex items-center gap-1.5"><Dot tone="violet" pulse /> 진행 중</span>}
                  </div>
                </div>
                {ok && (
                  <>
                    <CopyButton text={s.url} />
                    <a href={s.url} target="_blank" rel="noreferrer" className="rounded-md p-1 text-slate-400 hover:bg-white/5 hover:text-white" title="열기"><ExternalLink className="h-4 w-4" /></a>
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
