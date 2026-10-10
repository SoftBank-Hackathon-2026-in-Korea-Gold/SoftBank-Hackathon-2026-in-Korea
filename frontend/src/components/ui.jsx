import React, { useState } from 'react';
import { Check, Copy, Square } from 'lucide-react';
import { TONE } from '../lib/style';


export function Card({ title, icon: Icon, right, children, className = '', bodyClass = '' }) {
  return (
    <section className={`rounded-2xl border border-white/5 bg-slate-900/60 shadow-xl shadow-black/20 backdrop-blur ${className}`}>
      {(title || right) && (
        <header className="flex items-center justify-between gap-3 border-b border-white/5 px-5 py-3">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-200">
            {Icon && <Icon className="h-4 w-4 text-slate-400" />} {title}
          </h2>
          <div className="flex items-center gap-2">{right}</div>
        </header>
      )}
      <div className={`p-5 ${bodyClass}`}>{children}</div>
    </section>
  );
}

export function Badge({ tone = 'slate', children, className = '' }) {
  return <span className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset ${TONE[tone].soft} ${className}`}>{children}</span>;
}

export function Dot({ tone = 'slate', pulse = false }) {
  return (
    <span className="relative inline-flex h-2 w-2">
      {pulse && <span className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-60 ${TONE[tone].dot}`} />}
      <span className={`relative inline-flex h-2 w-2 rounded-full ${TONE[tone].dot}`} />
    </span>
  );
}

export function Bar({ value, tone = 'violet' }) {
  const v = Math.max(0, Math.min(100, value ?? 0));
  const t = v >= 80 ? 'rose' : v >= 50 ? 'amber' : tone;
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-white/5">
      <div className={`h-full rounded-full transition-all duration-700 ${TONE[t].bar}`} style={{ width: `${v}%` }} />
    </div>
  );
}

export function CopyButton({ text, className = '' }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      title="복사"
      onClick={(e) => { e.preventDefault(); e.stopPropagation(); navigator.clipboard?.writeText(text); setDone(true); setTimeout(() => setDone(false), 1200); }}
      className={`rounded-md p-1 text-slate-500 hover:bg-white/5 hover:text-slate-200 ${className}`}
    >
      {done ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
    </button>
  );
}

export function StopButton({ onClick, busy }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      className="flex items-center gap-1 rounded-lg border border-rose-500/30 px-2 py-1 text-[11px] text-rose-300 hover:border-rose-400/60 hover:bg-rose-500/10 hover:text-rose-200 disabled:opacity-60"
    >
      <Square className="h-3.5 w-3.5" /> {busy ? '중지 중…' : '중지'}
    </button>
  );
}
