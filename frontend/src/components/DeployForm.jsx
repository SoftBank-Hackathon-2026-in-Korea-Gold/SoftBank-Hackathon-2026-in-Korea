import React from 'react';
import { Cloud, FolderGit2, Loader2, Network, Play, Server, Tag } from 'lucide-react';
import { TARGET_META } from '../lib/run';
import { Card } from './ui';
import { TONE } from '../lib/style';

const ICONS = { local: Server, cloudrun: Cloud, node: Network };
const PRESETS = [
  { label: '방명록 (DB)', value: 'sample-apps/guestbook' },
  { label: '고장 앱 (자가치유)', value: 'sample-apps/broken' },
  { label: 'GitHub 데모 저장소', value: 'https://github.com/Jeonsubb/cloudmorph-demo-guestbook' },
];

export default function DeployForm({ form, setForm, onDeploy, busy, fleetAvailable }) {
  const toggle = (t) => setForm((f) => ({ ...f, targets: { ...f.targets, [t]: !f.targets[t] } }));
  const selected = Object.keys(form.targets).filter((t) => form.targets[t]);
  return (
    <Card title="새 배포" icon={Play}>
      <div className="space-y-4">
        <div>
          <label className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-slate-400"><FolderGit2 className="h-3.5 w-3.5" /> 소스 · 서버 경로, ZIP, 또는 GitHub URL</label>
          <input
            value={form.source}
            onChange={(e) => setForm((f) => ({ ...f, source: e.target.value }))}
            placeholder="https://github.com/owner/repo"
            className="w-full rounded-xl border border-white/10 bg-black/30 px-3.5 py-2.5 font-mono text-[13px] text-slate-100 placeholder:text-slate-600 focus:border-violet-500/60 focus:outline-none focus:ring-2 focus:ring-violet-500/20"
          />
          <div className="mt-2 flex flex-wrap gap-1.5">
            {PRESETS.map((p) => (
              <button key={p.value} type="button" onClick={() => setForm((f) => ({ ...f, source: p.value }))}
                className={`rounded-full border px-2.5 py-1 text-[11px] transition ${form.source === p.value ? 'border-violet-500/50 bg-violet-500/10 text-violet-200' : 'border-white/10 text-slate-400 hover:border-white/20 hover:text-slate-200'}`}>
                {p.label}
              </button>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-slate-400"><Tag className="h-3.5 w-3.5" /> 앱 이름 (선택)</label>
            <input value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value.toLowerCase() }))} placeholder="자동"
              className="w-full rounded-xl border border-white/10 bg-black/30 px-3 py-2 font-mono text-[13px] text-slate-100 placeholder:text-slate-600 focus:border-violet-500/60 focus:outline-none" />
          </div>
          <div>
            <label className="mb-1.5 block text-xs font-medium text-slate-400">브랜치 (Git일 때)</label>
            <input value={form.ref} onChange={(e) => setForm((f) => ({ ...f, ref: e.target.value }))} placeholder="기본 브랜치"
              className="w-full rounded-xl border border-white/10 bg-black/30 px-3 py-2 font-mono text-[13px] text-slate-100 placeholder:text-slate-600 focus:border-violet-500/60 focus:outline-none" />
          </div>
        </div>
        <p className="-mt-2 text-[11px] text-slate-500">같은 이름으로 다시 배포하면 같은 서비스가 새 버전으로 갱신됩니다.</p>

        <div>
          <label className="mb-1.5 block text-xs font-medium text-slate-400">배포 타깃</label>
          <div className="grid gap-2">
            {Object.entries(TARGET_META).map(([t, m]) => {
              const Icon = ICONS[t];
              const on = form.targets[t];
              const disabled = t === 'node' && !fleetAvailable;
              return (
                <button key={t} type="button" disabled={disabled} onClick={() => toggle(t)}
                  className={`flex items-center gap-3 rounded-xl border px-3 py-2.5 text-left transition ${on ? `${TONE[m.tone].border} bg-white/[0.04]` : 'border-white/5 hover:border-white/15'} ${disabled ? 'cursor-not-allowed opacity-40' : ''}`}>
                  <span className={`grid h-8 w-8 place-items-center rounded-lg ${on ? TONE[m.tone].soft : 'bg-white/5 text-slate-500'} ring-1 ring-inset ring-white/5`}><Icon className="h-4 w-4" /></span>
                  <span className="min-w-0 flex-1">
                    <span className={`block text-[13px] font-semibold ${on ? 'text-slate-100' : 'text-slate-400'}`}>{m.label}</span>
                    <span className="block truncate text-[11px] text-slate-500">{disabled ? '노드 풀이 설정되지 않음' : m.sub}</span>
                  </span>
                  <span className={`h-4 w-4 rounded-full border-2 ${on ? `border-transparent ${TONE[m.tone].dot}` : 'border-slate-600'}`} />
                </button>
              );
            })}
          </div>
        </div>

        <button type="button" onClick={onDeploy} disabled={busy || !form.source || selected.length === 0}
          className="flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-violet-900/40 transition hover:from-violet-500 hover:to-indigo-500 disabled:cursor-not-allowed disabled:from-slate-700 disabled:to-slate-700 disabled:shadow-none">
          {busy ? <><Loader2 className="h-4 w-4 animate-spin" /> 파이프라인 실행 중</> : <><Play className="h-4 w-4 fill-current" /> One Action Deploy · {selected.length}개 타깃</>}
        </button>
      </div>
    </Card>
  );
}
