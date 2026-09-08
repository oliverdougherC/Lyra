/**
 * PLA-501 cost measurement (not an assertion suite): what one commit of a streaming answer
 * costs, at the real feed cadence, read off the live page state, across four source sizes.
 * Run against a checkout and keep the printed report; a before/after pair at the same
 * sources is the evidence that the reveal schedule stays bounded.
 *
 * What a number means:
 * - `commitMs` is the wall time of ONE content append — the render, the commit, and the
 *   cascade layout effect React ran for it. It excludes the deliberate pause between
 *   chunks (that is the network, not the page), which is why it is kept separate from the
 *   playback wall time and never averaged into it.
 * - `styleWrites` / `inheritanceScans` are what that commit's cascade pass itself did
 *   (zeroed per commit). `inheritanceScans` is the containment checks of the deadline
 *   inheritance — the nested pass over the historical ranges — while
 *   `newUnitsScheduled` is how many units the commit actually placed. Dividing the two is
 *   how a whole-commit cost is attributed to the work it did: a pass that scales with
 *   new units × historical ranges shows the ratio climbing as the answer grows; a
 *   bounded one stays flat.
 */
import { act, render } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import { StreamingMarkdown } from '@/components/chat/streaming-markdown'
import * as reveal from '@/components/chat/reveal'

type Work = {
  styleWrites?: number
  inheritanceScans?: number
  newUnitsScheduled?: number
  inheritedUnits?: number
  historicalRanges?: number
}
const work: Work = (reveal as { revealWork?: Work }).revealWork ?? {}

/** A representative short answer: a couple of sentences with one inline equation. */
const SHORT = [
  'The pole sits at $s=-2$, so the impulse response decays as $e^{-2t}$.',
  'It is stable: the decay is fast, and the residue at the pole is one.',
].join('\n')

/**
 * A representative medium answer: the shapes a real tutor reply carries — prose, inline
 * and display mathematics, a nested list, a blockquote, a code block, and a table —
 * roughly four hundred words.
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
  'slowest pole. The transform $H(s)$ therefore has one pole at $s=-2$ and one at $s=-3$',
  'both on the negative real axis, so the system is BIBO stable. The damping of each mode is',
  'exponential with rates $2$ and $3$, and the ratio of the weights is $|{-2}/1| = 2.$',
  '',
  'In short: factor, cover, read the table — the slowest pole decides what you see last.',
].join('\n')

/**
 * A long, math- and list-heavy synthetic answer. Every symbol carries its position in
 * the source, so no word repeats: a repeated core would let two units share a key and
 * make the schedule's book-keeping read the same slot twice, which is not what a real
 * long reply does. The shapes mirror a real tutor reply — prose, display and inline
 * mathematics, nested bullets — at roughly an order of magnitude past a typical answer.
 */
function syntheticAnswer(targetChars: number): string {
  const parts: string[] = []
  let chars = 0
  let sentence = 0
  while (chars < targetChars) {
    if (sentence % 12 === 0) {
      // A display equation roughly every dozen sentences.
      const block = `$$S_{${sentence}}(x)=\\sum_{k=0}^{${8 + (sentence % 5)}} \\frac{(-1)^{k} x^{k}}{\\text{${(sentence % 7) + 1}}} + c_{${sentence}}$$\n\n`
      parts.push(block)
      chars += block.length
    } else if (sentence % 7 === 0) {
      // A short nested list roughly every seventh.
      const block = [
        `- Term ${sentence}: the coefficient $a_{${sentence}}$ controls the ${sentence % 2 ? 'rise' : 'decay'} rate`,
        `  - derived from $S_{${sentence}}$ at $x=0$`,
        `  - bounded by $\\frac{1}{${(sentence % 3) + 2}}$ on the interval`,
        '',
      ].join('\n')
      parts.push(block)
      chars += block.length
    } else {
      const block =
        `The ${['mode', 'residue', 'envelope', 'tail', 'kernel', 'axis'][sentence % 6]} indexed ${sentence} ` +
        `carries $a_{${sentence}}$ against the bound $b_{${(sentence + 1) % 97}}$, ` +
        `and the check at $x_{${sentence % 11}}$ keeps the row monotone. ` +
        `Reading the ${['slopes', 'weights', 'poles', 'terms'][sentence % 4]} in order, ` +
        `the ${['slowest', 'leading', 'dominant', 'final'][sentence % 4]} one, term $S_{${sentence}}$, ` +
        `decides the limit, which closes the step.\n`
      parts.push(block)
      chars += block.length
    }
    sentence += 1
  }
  return parts.join('')
}

const LARGE = syntheticAnswer(12_000)
const XLARGE = syntheticAnswer(18_000)

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
  newUnitsScheduled: number
  inheritedUnits: number
  historicalRanges: number
}

type SizeReport = {
  source: string
  length: number
  chunks: number
  units: number
  wallMs: number
  commitMs: { sum: number; mean: number; max: number; p95: number }
  work: {
    styleWrites: number
    inheritanceScans: number
    newUnitsScheduled: number
    inheritedUnits: number
    historicalRangesMax: number
  }
  scansPerNewUnit: number | null
  styleWritesPerCommit: number
  worstCommit: { n: number; contentLen: number; commitMs: number }
}

/**
 * Feeds the text chunk by chunk with real pauses between chunks and records what each
 * commit cost. Each `act` flushes the render, the commit, and the cascade layout effect,
 * so when it returns `work` holds the counters that commit's pass accumulated — and the
 * pause after it is network time, measured separately.
 */
async function feed(text: string, chunkMs: number) {
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
      newUnitsScheduled: work.newUnitsScheduled ?? 0,
      inheritedUnits: work.inheritedUnits ?? 0,
      historicalRanges: work.historicalRanges ?? 0,
    })
    await new Promise<void>((resolve) => setTimeout(resolve, chunkMs))
  }
  const wallMs = performance.now() - start
  const units = document.querySelectorAll('[data-stream-word]').length
  view.unmount()
  return { commits, wallMs, units }
}

function summarize(
  name: string,
  text: string,
  measured: Awaited<ReturnType<typeof feed>>,
): SizeReport {
  const { commits, wallMs, units } = measured
  const commitTimes = commits.map((c) => c.commitMs).sort((a, b) => a - b)
  const sum = commitTimes.reduce((a, b) => a + b, 0)
  const worst = commits.reduce((a, c) => (c.commitMs > a.commitMs ? c : a), commits[0]!)
  const scans = commits.reduce((a, c) => a + c.inheritanceScans, 0)
  const newUnits = commits.reduce((a, c) => a + c.newUnitsScheduled, 0)
  return {
    source: name,
    length: text.length,
    chunks: commits.length,
    units,
    wallMs: Math.round(wallMs),
    commitMs: {
      sum: Math.round(sum * 100) / 100,
      mean: Math.round((sum / Math.max(1, commits.length)) * 100) / 100,
      max: Math.round(worst.commitMs * 100) / 100,
      p95: Math.round(commitTimes[Math.floor(0.95 * commitTimes.length)]! * 100) / 100,
    },
    work: {
      styleWrites: commits.reduce((a, c) => a + c.styleWrites, 0),
      inheritanceScans: scans,
      newUnitsScheduled: newUnits,
      inheritedUnits: commits.reduce((a, c) => a + c.inheritedUnits, 0),
      historicalRangesMax: commits.reduce((a, c) => Math.max(a, c.historicalRanges), 0),
    },
    scansPerNewUnit: newUnits > 0 ? Math.round((scans / newUnits) * 100) / 100 : null,
    styleWritesPerCommit:
      Math.round(
        (commits.reduce((a, c) => a + c.styleWrites, 0) / Math.max(1, commits.length)) * 100,
      ) / 100,
    worstCommit: {
      n: worst.n,
      contentLen: worst.contentLen,
      commitMs: Math.round(worst.commitMs * 100) / 100,
    },
  }
}

describe('PLA-501 streaming cost', () => {
  it('short source at feed cadence', async () => {
    const feeded = await feed(SHORT, 25)
    const report = summarize('short', SHORT, feeded)
    console.log(`COST-REPORT ${JSON.stringify(report)}`)
    expect(feeded.commits.length, 'no commits were recorded').toBeGreaterThan(10)
  }, 30_000)

  it('medium source at feed cadence', async () => {
    const feeded = await feed(LONG, 40)
    const report = summarize('medium', LONG, feeded)
    console.log(`COST-REPORT ${JSON.stringify(report)}`)
    expect(feeded.commits.length, 'no commits were recorded').toBeGreaterThan(10)
    expect(feeded.units, 'no units on screen').toBeGreaterThan(50)
  }, 60_000)

  it('large synthetic source at feed cadence', async () => {
    const feeded = await feed(LARGE, 25)
    const report = summarize('large', LARGE, feeded)
    console.log(`COST-REPORT ${JSON.stringify(report)}`)
    expect(feeded.commits.length, 'no commits were recorded').toBeGreaterThan(100)
    expect(feeded.units, 'no units on screen').toBeGreaterThan(200)
  }, 120_000)

  it('xlarge synthetic source at feed cadence', async () => {
    const feeded = await feed(XLARGE, 25)
    const report = summarize('xlarge', XLARGE, feeded)
    console.log(`COST-REPORT ${JSON.stringify(report)}`)
    expect(feeded.commits.length, 'no commits were recorded').toBeGreaterThan(100)
    expect(feeded.units, 'no units on screen').toBeGreaterThan(200)
  }, 180_000)
})
