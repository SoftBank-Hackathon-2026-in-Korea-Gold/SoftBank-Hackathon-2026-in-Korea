import { describe, expect, it } from 'vitest';
import { applyEvent, emptyRun } from './run';

const patch = { attempt: 1, category: 'missing_dependency', source: 'rule', diff: '-old\n+new', before: 'old', after: 'new' };
const event = (run, type, payload, ts, stage) => applyEvent(run, type, payload, { ts, stage });

describe('SSE repair records', () => {
  it('tags patches by the deploying target and deduplicates event replay', () => {
    let run = event(emptyRun('job'), 'stage', { target: 'local' }, 1, 'deploy');
    run = event(run, 'heal_diff', patch, 2);
    expect(event(run, 'heal_diff', patch, 2)).toBe(run);
    run = event(run, 'stage', { target: 'cloudrun' }, 3, 'deploy');
    run = event(run, 'heal_diff', { ...patch, source: 'llm' }, 4);
    expect(run.patches).toEqual([{ ...patch, target: 'local' }, { ...patch, source: 'llm', target: 'cloudrun' }]);
  });
  it('restores missing final records without duplicating streamed patches', () => {
    let run = event(emptyRun('job'), 'stage', { target: 'local' }, 1, 'deploy');
    run = event(run, 'heal_diff', patch, 2);
    const second = { ...patch, attempt: 2, diff: '-new\n+fixed' };
    run = event(run, 'done', { target: 'local', records: [patch, second], url: 'https://ok' }, 3);
    expect(run.patches).toEqual([{ ...patch, target: 'local' }, { ...second, target: 'local' }]);
    run = event(run, 'error', { target: 'cloudrun', records: [patch] }, 4);
    expect(run.patches.at(-1).target).toBe('cloudrun');
  });
  it('keeps an unapplied source suggestion available for review', () => {
    const run = event(emptyRun('job'), 'log', { kind: 'source_suggestion', path: 'app.py', diff: patch.diff }, 1);
    expect(run.suggestion).toEqual({ path: 'app.py', diff: patch.diff });
    expect(run.logs[0].text).toContain('적용 안 함');
    expect(run.patches).toEqual([]);
  });
});
