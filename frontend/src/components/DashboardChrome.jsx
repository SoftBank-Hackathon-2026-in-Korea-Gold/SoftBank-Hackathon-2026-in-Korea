import React from 'react';
import { Boxes, Cloud, ExternalLink, Maximize, Minimize, Play, Rocket, Settings2, Square, Terminal } from 'lucide-react';
import { TARGET_META, stepStatus } from '../lib/run';
import { activeStepLabel, liveEndpoints, pendingInput, recentKeyLogs } from '../lib/dashboard';
import { fmtDur, fmtTime } from '../lib/style';
import { Dot } from './ui';

const STEPS = { analyze: '분석', deploy: '배포', heal: '자가치유', live: '공개 URL' };
const PHASES = { pending: '대기', deploying: '배포 중', healing: '치유 중', done: '완료', failed: '실패', cancelled: '중단' };

export function DashboardHeader({ scene, onScene, healthy, onPanel, fullscreen, onFullscreen }) {
  return (
    <header className="dashboard-header">
      <div className="dashboard-brand"><Cloud size={25} /><h1>CloudMorph</h1></div>
      <div className="scene-tabs" role="tablist" aria-label="3D 장면">
        {[['journey', '배포 여정', Rocket], ['fleet', '노드 풀', Boxes]].map(([id, label, Icon]) => (
          <button key={id} id={`scene-tab-${id}`} role="tab" aria-selected={scene === id} aria-controls="scene-view" tabIndex={scene === id ? 0 : -1}
            onClick={() => onScene(id)} onKeyDown={(e) => {
              if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) {
                e.preventDefault();
                const next = e.key === 'Home' ? 'journey' : e.key === 'End' ? 'fleet' : scene === 'journey' ? 'fleet' : 'journey';
                onScene(next); document.getElementById(`scene-tab-${next}`)?.focus();
              }
            }}><Icon size={15} />{label}</button>
        ))}
      </div>
      <div className="header-actions">
        <span className="connection-status" title={`백엔드 ${healthy ? '연결됨' : healthy === false ? '끊김' : '확인 중'}`}>
          <Dot tone={healthy ? 'emerald' : healthy === false ? 'rose' : 'slate'} /><span>백엔드 {healthy ? '연결됨' : healthy === false ? '끊김' : '확인 중'}</span>
        </span>
        <button className="dashboard-button primary" onClick={() => onPanel('deploy')}><Play size={14} />새 배포</button>
        <button className="dashboard-button" aria-label="상세·관리" onClick={() => onPanel('details')}><Settings2 size={15} /><span>상세·관리</span></button>
        <button className="dashboard-button icon-button" aria-label={fullscreen ? '전체화면 종료' : '전체화면'} onClick={onFullscreen} disabled={!document.fullscreenEnabled}>
          {fullscreen ? <Minimize size={16} /> : <Maximize size={16} />}
        </button>
      </div>
    </header>
  );
}

export function RunHUD({ run, now, notice, onPanel, onCancel, cancelling, celebrate }) {
  const steps = stepStatus(run);
  const pending = pendingInput(run);
  const elapsed = run.status === 'idle' ? null : Math.max(0, (run.finishedAt || now) - run.startedAt);
  return (
    <section className="run-hud" aria-label="배포 상태">
      <div className="run-headline">
        <div className="run-title"><span className={`status-light status-${run.status}`} /><strong>{celebrate ? 'AI 자가치유 성공' : activeStepLabel(run)}</strong>
          <span className="run-identity">{run.name || run.id || 'One Action, Infinite Clouds'}</span>
        </div>
        <div className="run-actions">
          {elapsed != null && <span className="elapsed">{fmtDur(elapsed)}</span>}
          {pending && <button className="dashboard-button attention" onClick={() => onPanel('details')}>입력 필요</button>}
          {run.status === 'running' && run.id && <button className="dashboard-button danger" disabled={cancelling} onClick={onCancel}><Square size={12} />{cancelling ? '중단 중…' : '중단'}</button>}
        </div>
      </div>
      <div className="run-progress">
        <ol className="compact-steps">{Object.entries(STEPS).map(([id, label], index) => <li key={id} data-state={steps[id]}><span>{index + 1}</span>{label}</li>)}</ol>
        <div className="target-summary">{run.targets.map((target) => <span key={target} data-phase={run.targetState[target]?.phase}>{TARGET_META[target]?.label || target} · {PHASES[run.targetState[target]?.phase] || '대기'}</span>)}</div>
      </div>
      {(pending || run.error || notice) && <div className={`hud-message ${run.error ? 'error' : ''}`} role={run.error ? 'alert' : 'status'}>
        {pending ? pending.label : run.error || notice}
      </div>}
    </section>
  );
}

export function ActivityDock({ run, project, onPanel }) {
  const logs = recentKeyLogs(run.logs);
  const urls = liveEndpoints(run, project);
  return (
    <footer className="activity-dock" aria-label="핵심 로그와 공개 URL">
      <div className="dock-heading"><span><Terminal size={13} />LIVE ACTIVITY</span><button onClick={() => onPanel('details')}>전체 로그 보기</button></div>
      <ol className="key-logs" aria-live="polite" aria-relevant="additions text">
        {logs.length === 0 ? <li className="empty-log">새 배포를 눌러 시작하세요. 진행 상황이 이곳에 표시됩니다.</li> : logs.map((log) => (
          <li key={log.id} data-kind={log.kind}><time>{fmtTime(log.at)}</time><span title={log.text}>{log.text}</span></li>
        ))}
      </ol>
      {urls.length > 0 && <div className="live-links">{urls.map(({ target, label, url }) => <a key={target} href={url} target="_blank" rel="noopener noreferrer"><ExternalLink size={12} />{label} 열기</a>)}</div>}
    </footer>
  );
}
