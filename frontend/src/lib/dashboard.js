import { TARGET_META, stepStatus } from './run'

export function pendingInput(run) {
  if (run.envRequest) return { key: `${run.id}:env`, label: '배포에 필요한 값 입력' }
  const q = run.question
  if (q?.ask && !q.choice) return { key: `${run.id}:question:${q.question_id}`, label: '자가치유 진행 여부 선택' }
  return null
}

export function recentKeyLogs(logs, limit = 3) {
  return logs.filter((l) => l.kind !== 'info' && l.kind !== 'note').slice(-limit)
}

export function liveEndpoints(run, project) {
  return run.targets.flatMap((target) => {
    const state = run.targetState[target]
    const stopped = project?.live && project.last_status !== 'running' && !project.live[target]
    return state?.phase === 'done' && !stopped && /^https?:\/\//i.test(state.url || '')
      ? [{ target, label: TARGET_META[target]?.label || target, url: state.url }]
      : []
  })
}

export function activeStepLabel(run) {
  if (pendingInput(run)) return '입력 대기'
  const labels = { analyze: '코드 분석', deploy: '빌드 & 배포', heal: 'AI 자가치유', live: '공개 URL' }
  const steps = stepStatus(run)
  return labels[Object.keys(steps).find((key) => steps[key] === 'active')] || {
    idle: '소스를 준비해 주세요', completed: '모든 타깃 배포 완료', partial_failure: '일부 타깃 실패',
    failed: '배포 실패', cancelled: '배포 중단', running: '배포 준비 중',
  }[run.status]
}
