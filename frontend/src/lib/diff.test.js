import { describe, expect, it } from 'vitest';
import { diffRows } from './diff';

describe('review diff labels', () => {
  it('names every file and retains before and after content', () => {
    const rows = diffRows('--- Dockerfile (before)\n+++ Dockerfile (after)\n@@ -1 +1 @@\n-RUN old\n+RUN new\n--- app.py (before)\n+++ app.py (suggested)\n@@ -1 +1 @@\n---removed text\n+++added text');
    expect(rows.filter((r) => r.kind === 'file').map((r) => r.text)).toEqual(['Dockerfile', 'app.py']);
    expect(rows).toContainEqual({ kind: 'removed', text: '---removed text' });
    expect(rows).toContainEqual({ kind: 'added', text: '+++added text' });
  });
  it('retains header-like source text outside a real header pair', () => {
    expect(diffRows('--- source\n+++ source\n unchanged')).toEqual([
      { kind: 'removed', text: '--- source' }, { kind: 'added', text: '+++ source' }, { kind: 'context', text: ' unchanged' },
    ]);
  });
});
