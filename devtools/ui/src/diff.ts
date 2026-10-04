/**
 * Text diffs for the workbench, pure and synchronous like `lineage.ts`, so the
 * node test runner can load it without a JSX transform.
 */

/**
 * A minimal unified diff of two texts, in the shape `SpecDiff` renders: only
 * the changed lines and two lines of context around them. A pending proposal
 * has no recorded diff yet, and a rewritten prompt shown whole hides the one
 * line that changed.
 */
export function lineDiff(before: string, after: string, context = 2): string {
  const a = before.split('\n')
  const b = after.split('\n')
  // Longest common subsequence table, bottom-up.
  const lcs: number[][] = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0))
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1])
    }
  }
  const ops: { kind: ' ' | '-' | '+'; line: string }[] = []
  let i = 0
  let j = 0
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      ops.push({ kind: ' ', line: a[i] })
      i++
      j++
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      ops.push({ kind: '-', line: a[i++] })
    } else {
      ops.push({ kind: '+', line: b[j++] })
    }
  }
  while (i < a.length) ops.push({ kind: '-', line: a[i++] })
  while (j < b.length) ops.push({ kind: '+', line: b[j++] })

  const keep = ops.map((_, index) =>
    ops.slice(Math.max(0, index - context), index + context + 1).some((near) => near.kind !== ' '),
  )
  const out: string[] = []
  ops.forEach((op, index) => {
    if (!keep[index]) {
      if (out[out.length - 1] !== '@@ …') out.push('@@ …')
      return
    }
    out.push(`${op.kind}${op.line}`)
  })
  return out.join('\n')
}
