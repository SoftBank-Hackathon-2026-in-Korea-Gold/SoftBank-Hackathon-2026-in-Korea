import React from 'react';
import { Zap, Cloud, Cpu, ExternalLink, Globe, Network, Server, Wrench } from 'lucide-react';
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
