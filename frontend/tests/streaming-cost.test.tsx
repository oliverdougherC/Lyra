/**
 * PLA-501 cost measurement (not an assertion suite): the per-commit work of streaming a
 * representative short and a representative long answer, at the real feed cadence, read
 * off the live page state. Run against a checkout and keep the printed report; the
 * before/after pair is the evidence that the reveal schedule stays bounded.
 *
 * What a number means: `commitMs` is the wall time of one content append — the render, the
 * commit, and the cascade pass React ran for it; `styleWrites` / `inheritanceScans` are the
 * counters that commit's cascade pass itself performed (zeroed per commit).
 */
import { act, render } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import { StreamingMarkdown } from '@/components/chat/streaming-markdown'
import * as reveal from '@/components/chat/reveal'

type Work = {
  styleWrites?: number
  inheritanceScans?: number
}
const work: Work = (reveal as { revealWork?: Work }).revealWork ?? {}

/** A representative short answer: a couple of sentences with one inline equation. */
const SHORT = [
  'The pole sits at $s=-2$, so the impulse response decays as $e^{-2t}$.',
  'It is stable: the decay is fast, and the residue at the pole is one.',
].join('\n')

/**
 * A representative long answer: the shapes a real tutor reply carries — prose, inline and
 * display mathematics, a nested list, a blockquote, a code block, and a table — roughly
 * four hundred words.
 */
const LONG = [
  'The Laplace transform of the system is',
  '',
  '$$H(s)=\\frac{s+1}{(s+2)(s+3)}$$',
  '',
  'and its partial-fraction form is',
  '',
  '$$H(s)=\\frac{1}{s+2}-\\frac{2}{s+3},$$',
  '',
  'so the impulse response is the sum of two exponentials:',
  '',
  '$$h(t)=e^{-2t}-2e^{-3t}.$$ ',
  '',
  'The steps, with the notation this course uses:',
  '',
  '- Factor the denominator',
  '  - roots at $s=-2$ and $s=-3$; $s=-2$ is the slow mode',
  '  - the faster mode is $e^{-3t}$, which is why the tail is light',
  '- Cover the residue at each pole',
  '  - at $s=-2$: $\\operatorname{Res} = \\frac{1}{-2+3} = 1$',
  '  - at $s=-3$: $\\operatorname{Res} = \\frac{1}{-3+2} = -1$, doubled by the sign',
  '',
  '> A common slip: the coefficient at $s=-3$ carries the sign of the denominator.',
  '',
  'The steady-state check:',
  '',
  '```js',
  '// final value: lim s->0 of s*H(s)',
  'const h0 = (1 * 0 + 1) / ((0 + 2) * (0 + 3))',
  '```',
  '',
  '| mode | decay | weight |',
  '| - | - | - |',
  '| $e^{-2t}$ | $2$ s$^{-1}$ | $1$ |',
  '| $e^{-3t}$ | $3$ s$^{-1}$ | $-2$ |',
  '',
  'Reading the table: the first row dominates the long-time behavior, which is exactly the',
  'slowest pole. The transform $H(s)$ therefore has one pole at $s=-2$ and one at $s=-3$,',
  'both on the negative real axis, so the system is BIBO stable. The damping of each mode is',
  'exponential with rates $2$ and $3$, and the ratio of the weights is $|{-2}/1| = 2$.',
  '',
  'In short: factor, cover, read the table — the slowest pole decides what you see last.',
].join('\n')

/** The real feed cadence: characters, then words, then bursts. */
function feedPlan(text: string): string[] {
  const chunks: string[] = []
  let i = 0
  while (i < text.length) {
    if (i < 60) {
      chunks.push(text.slice(i, i + 2))
      i += 2
    } else if (i < 300) {
      const space = text.indexOf(' ', i)
      const end = space === -1 ? text.length : space + 1
      chunks.push(text.slice(i, end))
      i = end
    } else {
      chunks.push(text.slice(i, i + 40))
      i += 40
    }
  }
  return chunks
}

type Commit = {
  n: number
  contentLen: number
  commitMs: number
  styleWrites: number
  inheritanceScans: number
}

/**
 * Feeds the text chunk by chunk with real pauses between chunks and records what each
 * commit cost. Each `act` flushes the render, the commit, and the cascade layout effect,
 * so when it returns `work` holds the counters that commit's pass accumulated.
 */
async function feed(
  text: string,
  chunkMs: number,
): Promise<{ commits: Commit[]; totalMs: number; finalUnits: number }> {
  let content = ''
  let force = () => {}
  function Recorder() {
    const [, setVersion] = useState(0)
    force = () => setVersion((n) => n + 1)
    return <StreamingMarkdown content={content} streaming onRevealComplete={() => {}} />
  }
  const view = render(<Recorder />)
  act(() => {})
  const commits: Commit[] = []
  const start = performance.now()
  for (const chunk of feedPlan(text)) {
    content += chunk
    const t0 = performance.now()
    act(() => force())
    commits.push({
      n: commits.length + 1,
      contentLen: content.length,
      commitMs: performance.now() - t0,
      styleWrites: work.styleWrites ?? 0,
      inheritanceScans: work.inheritanceScans ?? 0,
    })
    await new Promise<void>((resolve) => setTimeout(resolve, chunkMs))
  }
  const totalMs = performance.now() - start
  const finalUnits = document.querySelectorAll('[data-stream-word]').length
  view.unmount()
  return { commits, totalMs, finalUnits }
}

async function report(name: string, text: string, chunkMs: number) {
  const { commits, totalMs, finalUnits } = await feed(text, chunkMs)
  const worst = commits.reduce((a, c) => (c.commitMs > a.commitMs ? c : a), commits[0]!)
  const reportOut = {
    source: name,
    length: text.length,
    chunks: commits.length,
    totalMs: Math.round(totalMs),
    avgCommitMs: Math.round((totalMs / Math.max(1, commits.length)) * 100) / 100,
    worstCommit: {
      n: worst.n,
      contentLen: worst.contentLen,
      commitMs: Math.round(worst.commitMs * 100) / 100,
    },
    totalStyleWrites: commits.reduce((a, c) => a + c.styleWrites, 0),
    totalInheritanceScans: commits.reduce((a, c) => a + c.inheritanceScans, 0),
    finalUnits,
  }
  console.log(`COST-REPORT ${JSON.stringify(reportOut)}`)
  expect(commits.length, 'no commits were recorded').toBeGreaterThan(10)
}

describe('PLA-501 streaming cost', () => {
  it('short source at feed cadence', async () => {
    await report('short', SHORT, 25)
  }, 30_000)
  it('long source at feed cadence', async () => {
    await report('long', LONG, 40)
  }, 60_000)
})
