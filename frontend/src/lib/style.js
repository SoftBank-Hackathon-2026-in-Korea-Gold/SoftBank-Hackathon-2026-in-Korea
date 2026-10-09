// Tailwind (Play CDN) only styles class names it can see, so every tone variant is spelled out.
export const TONE = {
  emerald: { text: 'text-emerald-300', soft: 'bg-emerald-500/10 text-emerald-300 ring-emerald-500/30', dot: 'bg-emerald-400', bar: 'bg-emerald-400', border: 'border-emerald-500/40' },
  indigo: { text: 'text-indigo-300', soft: 'bg-indigo-500/10 text-indigo-300 ring-indigo-500/30', dot: 'bg-indigo-400', bar: 'bg-indigo-400', border: 'border-indigo-500/40' },
  sky: { text: 'text-sky-300', soft: 'bg-sky-500/10 text-sky-300 ring-sky-500/30', dot: 'bg-sky-400', bar: 'bg-sky-400', border: 'border-sky-500/40' },
  violet: { text: 'text-violet-300', soft: 'bg-violet-500/10 text-violet-300 ring-violet-500/30', dot: 'bg-violet-400', bar: 'bg-violet-400', border: 'border-violet-500/40' },
  amber: { text: 'text-amber-300', soft: 'bg-amber-500/10 text-amber-300 ring-amber-500/30', dot: 'bg-amber-400', bar: 'bg-amber-400', border: 'border-amber-500/40' },
  rose: { text: 'text-rose-300', soft: 'bg-rose-500/10 text-rose-300 ring-rose-500/30', dot: 'bg-rose-400', bar: 'bg-rose-400', border: 'border-rose-500/40' },
  slate: { text: 'text-slate-300', soft: 'bg-slate-500/10 text-slate-300 ring-slate-500/30', dot: 'bg-slate-500', bar: 'bg-slate-400', border: 'border-slate-700' },
};

const pad = (n) => String(n).padStart(2, '0');
export const fmtTime = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
export const fmtAgo = (ts) => {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  return s < 60 ? `${s}초 전` : s < 3600 ? `${Math.round(s / 60)}분 전` : `${Math.round(s / 3600)}시간 전`;
};
export const fmtDur = (ms) => (ms < 60000 ? `${Math.round(ms / 1000)}초` : `${Math.floor(ms / 60000)}분 ${Math.round((ms % 60000) / 1000)}초`);
