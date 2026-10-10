import React, { useEffect, useState } from 'react';
import { Loader2, Plus, RotateCcw, Save, Trash2 } from 'lucide-react';
import { getProjectEnv, putProjectEnv } from '../api';
import { Badge } from './ui';

const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
const inputCls = 'w-full rounded-lg border border-white/10 bg-black/30 px-3 py-1.5 font-mono text-[12px] text-slate-100 placeholder:text-slate-600 focus:border-violet-500/60 focus:outline-none';
const iconBtn = 'rounded-md p-1.5 text-slate-400 hover:bg-white/5 hover:text-rose-300';

// Saved values never come back from the backend: rows show keys only and a typed value replaces the old one.
// Keys are NAME (runtime env) or build:NAME (build input); names added here are runtime env.
export default function EnvEditor({ project, onRedeploy }) {
  const [keys, setKeys] = useState(undefined); // undefined: loading, null: not stored on this server
  const [values, setValues] = useState({});
  const [removed, setRemoved] = useState(new Set());
  const [added, setAdded] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    getProjectEnv(project.name).then(setKeys).catch((e) => setError(e.message));
  }, [project.name]);

  const newRows = added.filter((a) => a.name || a.value);
  const badName = newRows.find((a) => !NAME_RE.test(a.name) || (keys || []).includes(a.name));
  const changes = {
    ...Object.fromEntries(Object.entries(values).filter(([k, v]) => v && !removed.has(k))),
    ...Object.fromEntries(newRows.filter((a) => a.value).map((a) => [a.name, a.value])),
  };
  const dirty = Object.keys(changes).length > 0 || removed.size > 0;

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      setKeys(await putProjectEnv(project.name, { values: changes, unset: [...removed] }));
      setValues({});
      setRemoved(new Set());
      setAdded([]);
      setSaved(true);
    } catch (e) {
      setError(e.message);
    }
    setBusy(false);
  };

  const toggleRemove = (k) => setRemoved((s) => {
    const next = new Set(s);
    if (next.has(k)) next.delete(k);
    else next.add(k);
    return next;
  });
  const editAdded = (i, field, v) => setAdded((xs) => xs.map((x, j) => (j === i ? { ...x, [field]: v } : x)));

  if (keys === null) {
    return <p className="px-5 pb-4 text-xs text-slate-500">이 서버는 GCP_PROJECT_ID가 없어 값을 저장하지 않습니다. 배포할 때마다 입력 카드로 받습니다.</p>;
  }
  return (
    <div className="space-y-2 px-5 pb-4">
      <p className="text-[11px] text-slate-500">GCP Secret Manager에 이 앱 이름으로 저장된 값입니다. 저장된 값은 다시 보여 주지 않고, 바꾼 값은 다음 배포부터 들어갑니다.</p>
      {keys === undefined && !error && <Loader2 className="h-4 w-4 animate-spin text-slate-500" />}
      {keys && keys.length === 0 && added.length === 0 && <p className="text-xs text-slate-500">저장된 값이 없습니다.</p>}
      {(keys || []).map((k) => {
        const build = k.startsWith('build:');
        return (
          <div key={k} className={`flex items-center gap-2 ${removed.has(k) ? 'opacity-40' : ''}`}>
            <span className="w-56 shrink-0 truncate font-mono text-[12px] text-slate-200">{build ? k.slice(6) : k}</span>
            <Badge tone={build ? 'indigo' : 'sky'} className="shrink-0">{build ? '빌드' : '실행'}</Badge>
            <input type="password" autoComplete="off" disabled={removed.has(k)} value={values[k] || ''} placeholder="새 값 (비우면 그대로)"
              onChange={(e) => setValues((v) => ({ ...v, [k]: e.target.value }))} className={inputCls} />
            <button type="button" title={removed.has(k) ? '삭제 취소' : '삭제'} onClick={() => toggleRemove(k)} className={iconBtn}>
              {removed.has(k) ? <RotateCcw className="h-3.5 w-3.5" /> : <Trash2 className="h-3.5 w-3.5" />}
            </button>
          </div>
        );
      })}
      {added.map((a, i) => (
        <div key={i} className="flex items-center gap-2">
          <input autoComplete="off" value={a.name} placeholder="NAME" onChange={(e) => editAdded(i, 'name', e.target.value)} className={`${inputCls} w-56 shrink-0`} />
          <Badge tone="sky" className="shrink-0">실행</Badge>
          <input type="password" autoComplete="off" value={a.value} placeholder="값" onChange={(e) => editAdded(i, 'value', e.target.value)} className={inputCls} />
          <button type="button" title="빼기" onClick={() => setAdded((xs) => xs.filter((_, j) => j !== i))} className={iconBtn}>
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      ))}
      {badName && <p className="text-xs text-rose-300">이름은 영문 · 숫자 · 밑줄만 쓰고 숫자로 시작할 수 없으며, 이미 있는 이름과 겹치면 안 됩니다: {badName.name || '(빈 이름)'}</p>}
      {error && <p className="text-xs text-rose-300">실패: {error}</p>}
      <div className="flex flex-wrap items-center gap-2 pt-1 text-[12px]">
        <button type="button" onClick={() => setAdded((xs) => [...xs, { name: '', value: '' }])}
          className="flex items-center gap-1 rounded-lg border border-white/10 px-2.5 py-1 text-slate-300 hover:border-white/20 hover:text-white">
          <Plus className="h-3.5 w-3.5" /> 추가
        </button>
        <button type="button" onClick={save} disabled={busy || !dirty || !!badName}
          className="flex items-center gap-1 rounded-lg bg-violet-600 px-2.5 py-1 font-semibold text-white hover:bg-violet-500 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400">
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />} 저장
        </button>
        {saved && !dirty && (
          <button type="button" onClick={() => onRedeploy(project)}
            className="flex items-center gap-1 rounded-lg border border-emerald-500/30 px-2.5 py-1 text-emerald-300 hover:bg-emerald-500/10">
            <RotateCcw className="h-3.5 w-3.5" /> 저장됨 · 지금 재배포
          </button>
        )}
      </div>
    </div>
  );
}
