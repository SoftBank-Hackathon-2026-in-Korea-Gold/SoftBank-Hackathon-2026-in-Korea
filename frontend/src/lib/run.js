// Pipeline state for one deployment, built only from SSE events (so a page opened mid-run, or attached to a
// GitHub-push deployment, replays to the same picture). EventSource reconnects replay history; `seen` dedupes.

export const TARGET_META = {
  local: { label: 'Local Docker', sub: '온프레미스 · cloudflared 터널', tone: 'emerald' },
  cloudrun: { label: 'Cloud Run', sub: '서버리스 컨테이너 · 블루그린', tone: 'indigo' },
  node: { label: 'Node Pool', sub: '멀티 클라우드 VM · 자동 확장', tone: 'sky' },
}

export function emptyRun(id = null, meta = {}) {
  const targets = meta.targets || []
  return {
    id,
    status: id ? 'running' : 'idle',
    source: meta.source || '',
    name: meta.name || null,
    trigger: meta.trigger || 'api',
    targets,
    startedAt: Date.now(),
    finishedAt: null,
    analysis: null,
    notes: [],
    currentTarget: null,
    targetState: Object.fromEntries(targets.map((t) => [t, freshTarget()])),
    logs: [],
    patches: [],
    seen: new Set(),
    error: null,
    question: null, // healer fixed the source: { question_id, ask, pr_url, pr_error, rationale, diff, choice, reason }
  }
}

const freshTarget = () => ({ phase: 'pending', url: null, attempts: 0, message: null, healed: false })
let seq = 0

function ensure(run, t) {
  if (!t) return
  if (!run.targetState[t]) run.targetState[t] = freshTarget()
  if (!run.targets.includes(t)) run.targets = [...run.targets, t]
}

function push(run, entry) {
  run.logs.push({ id: ++seq, at: entry.ts ? new Date(entry.ts * 1000) : new Date(), ...entry })
}

function parseSummary(line) {
  const kv = Object.fromEntries([...line.matchAll(/(\w+)=(\[[^\]]*\]|\S+)/g)].map((m) => [m[1], m[2]]))
  const models = (kv.service_models || '').replace(/[[\]'"]/g, '').split(',').map((s) => s.trim()).filter(Boolean)
  return { target: kv.target, language: kv.language, framework: kv.framework, port: kv.port, models }
}

export function applyEvent(prev, type, p, raw) {
  const key = `${type}|${raw?.ts ?? Math.random()}|${raw?.stage ?? ''}|${p.target ?? ''}|${p.attempt ?? ''}`
  if (prev.seen.has(key)) return prev
  const run = {
    ...prev,
    logs: [...prev.logs],
    patches: [...prev.patches],
    notes: [...prev.notes],
    targetState: { ...prev.targetState },
    seen: new Set(prev.seen).add(key),
  }
  const ts = raw?.ts
  const stage = raw?.stage

  switch (type) {
    case 'log': {
      const line = p.line || ''
      if (p.kind === 'question') {
        run.question = { ...p, choice: null, reason: null }
        const pr = p.pr_url ? `PR을 올렸어요 · ${p.pr_url}` : '소스를 고쳤어요 (PR 없음)'
        push(run, { ts, kind: 'heal', stage: 'heal', target: run.currentTarget, text: `${pr} — ${p.rationale}` })
      } else if (p.kind === 'answer') {
        run.question = run.question && { ...run.question, choice: p.choice, reason: p.reason }
        const why = { user: '사용자 선택', timeout: '응답 없음', 'github-push': 'push 배포' }[p.reason] || p.reason
        push(run, { ts, kind: 'info', stage: 'heal', text: `${p.choice === 'continue' ? '고친 소스로 계속 배포' : '이번 배포는 여기서 중단'} (${why})` })
      } else if (line.startsWith('analyzer: target=')) {
        run.analysis = parseSummary(line)
        const a = run.analysis
        push(run, { ts, kind: 'analyze', stage: 'analyze', text: `분석 완료 · 권장 타깃 ${a.target} · ${a.language}/${a.framework} · port ${a.port}` })
      } else if (line.startsWith('analyzer: ')) {
        run.notes.push(line.slice('analyzer: '.length))
        push(run, { ts, kind: 'note', stage: 'analyze', text: line.slice('analyzer: '.length) })
      } else if (line.startsWith('queue: ')) {
        push(run, { ts, kind: 'push', stage: 'queue', text: `대기 · 같은 앱의 이전 배포가 끝나면 시작합니다 (${line.slice(7)})` })
      } else if (line.startsWith('github push')) {
        run.trigger = 'github-push'
        push(run, { ts, kind: 'push', stage: 'trigger', text: line })
      } else {
        push(run, { ts, kind: 'info', stage: stage || 'log', text: line })
      }
      break
    }
    case 'stage': {
      if (stage === 'analyze') {
        push(run, { ts, kind: 'stage', stage, text: 'AI 검사관이 저장소를 읽는 중 · 언어 · 프레임워크 · 상태 저장 · DB · 포트' })
      } else if (stage === 'deploy') {
        const t = p.target
        ensure(run, t)
        run.currentTarget = t
        run.targetState[t] = { ...run.targetState[t], phase: 'deploying' }
        push(run, { ts, kind: 'stage', stage, target: t, text: '빌드 → 배포 → 헬스체크' })
      } else if (stage === 'heal') {
        const t = p.target || run.currentTarget
        ensure(run, t)
        run.targetState[t] = { ...run.targetState[t], phase: 'healing' }
        const first = (p.stderr || '').split('\n').find((l) => l.startsWith('[deployer]')) || ''
        push(run, { ts, kind: 'error', stage: 'deploy', target: t, text: `배포 실패 → 자가치유 에이전트로 전달 ${first.replace('[deployer] ', '· ')}`, detail: p.stderr })
      } else if (stage === 'redeploy') {
        const t = run.currentTarget
        if (t) run.targetState[t] = { ...run.targetState[t], attempts: p.attempt }
        push(run, { ts, kind: 'heal', stage: 'redeploy', target: t, text: `패치 #${p.attempt} 적용 → 재배포` })
      }
      break
    }
    case 'heal_diff': {
      const t = run.currentTarget
      run.patches.push({ ...p, target: t })
      push(run, {
        ts,
        kind: 'heal',
        stage: 'heal',
        target: t,
        text: `자가치유 #${p.attempt} · ${p.category} · ${p.source === 'rule' ? '규칙' : 'LLM'} — ${p.rationale}`,
        detail: p.error_excerpt,
      })
      break
    }
    case 'done': {
      const t = p.target || run.currentTarget
      ensure(run, t)
      const url = p.url || p.final_url
      run.targetState[t] = { ...run.targetState[t], phase: 'done', url, healed: Array.isArray(p.records) && p.records.length > 0 }
      push(run, { ts, kind: 'success', stage: 'done', target: t, text: p.summary ? `배포 완료 · ${p.summary}` : '배포 완료', url })
      break
    }
    case 'error': {
      const t = p.target
      const msg = p.message || p.summary || '실패'
      if (t) {
        ensure(run, t)
        run.targetState[t] = { ...run.targetState[t], phase: 'failed', message: msg }
        push(run, { ts, kind: 'error', stage: 'failed', target: t, text: msg })
      } else {
        run.error = msg
        run.status = 'failed'
        run.finishedAt = Date.now()
        push(run, { ts, kind: 'error', stage: 'failed', text: msg })
      }
      break
    }
    default:
  }

  const states = run.targets.map((t) => run.targetState[t]).filter(Boolean)
  if (run.status === 'running' && states.length && states.every((s) => s.phase === 'done' || s.phase === 'failed')) {
    run.status = states.every((s) => s.phase === 'done') ? 'completed' : states.some((s) => s.phase === 'done') ? 'partial_failure' : 'failed'
    run.finishedAt = Date.now()
  }
  return run
}

/** Which of the 4 pipeline steps is active / done, for the stepper. */
export function stepStatus(run) {
  const phases = Object.values(run.targetState).map((s) => s.phase)
  const anyStarted = phases.some((p) => p !== 'pending')
  const healed = run.patches.length > 0
  const finished = run.status !== 'running' && run.status !== 'idle'
  const failed = run.status === 'failed'
  return {
    analyze: run.status === 'idle' ? 'idle' : run.analysis || anyStarted ? 'done' : failed ? 'failed' : 'active',
    deploy: !anyStarted ? (finished && failed ? 'failed' : 'idle') : phases.some((p) => p === 'deploying') ? 'active' : 'done',
    heal: phases.some((p) => p === 'healing') ? 'active' : healed ? 'done' : finished ? 'skipped' : 'idle',
    live: finished ? (run.status === 'failed' ? 'failed' : run.status === 'partial_failure' ? 'partial' : 'done') : 'idle',
  }
}
