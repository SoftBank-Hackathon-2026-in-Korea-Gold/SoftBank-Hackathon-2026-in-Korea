// A removed line can itself start with "---". Recognize only unified-diff header pairs
// followed by a hunk, so source text never disappears from the review card.
export function diffRows(diff = '') {
  const lines = diff.split('\n');
  const rows = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith('--- ') && lines[i + 1]?.startsWith('+++ ') && lines[i + 2]?.startsWith('@@')) {
      const before = line.slice(4).replace(/ \(before\)$/, '');
      const after = lines[++i].slice(4).replace(/ \((after|suggested)\)$/, '');
      rows.push({ kind: 'file', text: before === after ? after : `${before} → ${after}` });
    } else {
      rows.push({ kind: line.startsWith('+') ? 'added' : line.startsWith('-') ? 'removed' : line.startsWith('@@') ? 'hunk' : 'context', text: line });
    }
  }
  return rows;
}
