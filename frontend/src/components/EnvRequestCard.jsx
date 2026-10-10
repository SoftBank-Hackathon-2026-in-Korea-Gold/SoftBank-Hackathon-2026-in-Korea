import React, { useState } from 'react';
import { KeyRound, Loader2, Play } from 'lucide-react';
import { Badge, Card } from './ui';

// The same name can be needed at build and at run time; one answer covers both.
function byName(items) {
  const merged = new Map();
  for (const it of items) {
    const prev = merged.get(it.name);
    merged.set(it.name, prev
      ? { ...prev, scopes: [...new Set([...prev.scopes, it.scope])], secret: prev.secret || it.secret, generate: prev.generate && it.generate, reason: prev.reason || it.reason, evidence: [...prev.evidence, ...it.evidence] }
      : { ...it, scopes: [it.scope] });
  }
  return [...merged.values()];
}

const SCOPE = { build: '빌드', runtime: '실행' };

export default function EnvRequestCard({ request, onSubmit }) {
  const items = byName(request.items);
  const [values, setValues] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const send = async (answer) => {
    setBusy(true);
    setError(null);
    try {
      await onSubmit(answer);
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  };

  return (
    <Card title="배포에 필요한 값" icon={KeyRound} right={<Badge tone="amber">입력 대기</Badge>} className="border-amber-500/30">
      <p className="mb-4 text-xs leading-5 text-slate-400">
        AI 검사관이 코드에서 아래 값을 찾았습니다. 넣은 값은 이번 배포에만 쓰고 로그 · 이벤트에 남기지 않습니다.
        {request.timeoutSec ? ` ${Math.round(request.timeoutSec / 60)}분 안에 답하지 않으면 배포를 멈춥니다.` : ''}
      </p>
      <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); send(values); }}>
        {items.map((it) => (
          <div key={it.name} className="rounded-xl border border-white/5 bg-black/20 p-3">
            <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
              <span className="font-mono text-[13px] font-semibold text-slate-100">{it.name}</span>
              {it.scopes.map((s) => <Badge key={s} tone={s === 'build' ? 'indigo' : 'sky'}>{SCOPE[s]}</Badge>)}
              {it.secret && <Badge tone="rose">비밀값</Badge>}
            </div>
            {it.reason && <p className="mb-1 text-[12px] text-slate-300">{it.reason}</p>}
            {it.evidence.length > 0 && <p className="mb-2 truncate font-mono text-[11px] text-slate-500">{it.evidence.join(' · ')}</p>}
            <input
              type={it.secret ? 'password' : 'text'}
              autoComplete="off"
              value={values[it.name] || ''}
              onChange={(e) => setValues((v) => ({ ...v, [it.name]: e.target.value }))}
              placeholder={it.generate ? '비워 두면 무작위 값을 만듭니다' : '값 입력'}
              className="w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2 font-mono text-[13px] text-slate-100 placeholder:text-slate-600 focus:border-amber-500/60 focus:outline-none"
            />
          </div>
        ))}
        {error && <p className="text-xs text-rose-300">전송 실패: {error}</p>}
        <div className="flex gap-2 pt-1">
          <button type="submit" disabled={busy}
            className="flex flex-1 items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-amber-600 to-orange-600 px-4 py-2.5 text-sm font-semibold text-white transition hover:from-amber-500 hover:to-orange-500 disabled:cursor-not-allowed disabled:from-slate-700 disabled:to-slate-700">
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4 fill-current" />} 값 넣고 배포 계속
          </button>
          <button type="button" disabled={busy} onClick={() => send({})}
            className="rounded-xl border border-white/10 px-4 py-2.5 text-sm text-slate-300 transition hover:border-white/20 hover:text-white disabled:cursor-not-allowed disabled:opacity-40">
            비워 두고 계속
          </button>
        </div>
      </form>
    </Card>
  );
}
