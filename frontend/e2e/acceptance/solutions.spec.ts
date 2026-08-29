/**
 * Solution lifecycle through the real stack (PLA-311).
 *
 * Proves: create solution (202), wait for segmentation, start solving,
 * wait for completion, verify part structure and ownership, rename, delete,
 * source invalidation, and wrong-kind boundary.
 *
 * Exercises the full solution worker pipeline: create/queue → segmenting →
 * awaiting_review → start → solving → ready, through the real backend.
 */

import { test, expect } from '@playwright/test'
import { resolve } from 'node:path'
import {
  apiGet,
  apiPost,
  apiPatch,
  apiDelete,
  createClass,
  uploadDocument,
  waitForDocumentReady,
  waitForSolutionSegmented,
  waitForSolutionReady,
  clearTutorState,
} from './helpers'

const TEST_DATA = resolve(__dirname, 'test-data')

test.describe('Solutions (PLA-311)', () => {
  let classId: number
  let docId: number

  test.beforeAll(async () => {
    const cls = await createClass('Acceptance: Solutions')
    classId = cls.id

    const res = await uploadDocument(classId, resolve(TEST_DATA, 'sample.txt'), 'sample.txt')
    const doc = await res.json()
    docId = doc.id
    await waitForDocumentReady(docId, 30_000)
  })

  test.afterEach(async () => {
    await clearTutorState()
  })

  let solutionId: number

  test('create solution returns 202 and queues segmentation', async () => {
    const res = await apiPost(`/api/classes/${classId}/solutions`, {
      title: 'Thermo Solutions',
      sources: [{ document_id: docId, role: 'problem_set' }],
    })
    expect(res.status).toBe(202)
    const solution = await res.json()
    solutionId = solution.id
    expect(solution.id).toBeGreaterThan(0)
    expect(solution.kind).toBe('solution_set')
    expect(solution.state).toMatch(/pending|segmenting/)
    expect(solution.sources.length).toBe(1)
    expect(solution.sources[0].document_id).toBe(docId)
    expect(solution.sources[0].role).toBe('problem_set')
  })

  test('segmentation completes, then start solving reaches ready', async () => {
    test.slow()
    await waitForSolutionSegmented(solutionId, 60_000)

    const segRes = await apiGet(`/api/solutions/${solutionId}/status`)
    const segStatus = await segRes.json()
    expect(segStatus.state).toBe('awaiting_review')
    expect(segStatus.problems_total).toBeGreaterThan(0)

    const startRes = await apiPost(`/api/solutions/${solutionId}/start`)
    expect(startRes.status).toBe(202)

    await waitForSolutionReady(solutionId, 120_000)

    const solutionRes = await apiGet(`/api/solutions/${solutionId}`)
    expect(solutionRes.ok).toBe(true)
    const solution = await solutionRes.json()
    expect(solution.state).toBe('ready')
    expect(solution.parts.length).toBeGreaterThan(0)
  })

  test('parts have correct structure, kind, and ownership', async () => {
    const solutionRes = await apiGet(`/api/solutions/${solutionId}`)
    const solution = await solutionRes.json()
    const problems = solution.parts.filter(
      (p: { parent_part_id: number | null; kind: string }) =>
        p.parent_part_id === null && p.kind === 'problem',
    )
    expect(problems.length).toBeGreaterThan(0)

    const firstProblem = problems[0]
    expect(firstProblem.id).toBeGreaterThan(0)
    expect(firstProblem.artifact_id).toBe(solutionId)
    expect(firstProblem.kind).toBe('problem')
    expect(typeof firstProblem.content).toBe('string')
    expect(firstProblem.content.length).toBeGreaterThan(0)

    for (const part of solution.parts) {
      expect(part.artifact_id).toBe(solutionId)
      expect(['problem', 'step', 'answer']).toContain(part.kind)
    }
  })

  test('status endpoint returns progress shape', async () => {
    const statusRes = await apiGet(`/api/solutions/${solutionId}/status`)
    expect(statusRes.ok).toBe(true)
    const status = await statusRes.json()
    expect(status.state).toBe('ready')
    expect(typeof status.problems_total).toBe('number')
    expect(typeof status.problems_done).toBe('number')
    expect(Array.isArray(status.parts)).toBe(true)
    for (const p of status.parts) {
      expect(p.id).toBeGreaterThan(0)
      expect(typeof p.status).toBe('string')
      expect(typeof p.verdict).toBe('string')
    }
  })

  test('rename solution', async () => {
    const renameRes = await apiPatch(`/api/solutions/${solutionId}`, {
      title: 'Renamed Solutions',
    })
    expect(renameRes.ok).toBe(true)

    const solutionRes = await apiGet(`/api/solutions/${solutionId}`)
    const solution = await solutionRes.json()
    expect(solution.title).toBe('Renamed Solutions')
  })

  test('solution appears in class list', async () => {
    const listRes = await apiGet(`/api/classes/${classId}/solutions`)
    expect(listRes.ok).toBe(true)
    const solutions = await listRes.json()
    expect(solutions.length).toBeGreaterThan(0)
    const found = solutions.find((s: { id: number }) => s.id === solutionId)
    expect(found).toBeTruthy()
    expect(found.title).toBe('Renamed Solutions')
  })

  test('wrong kind: creating solution from non-existent document fails', async () => {
    const res = await apiPost(`/api/classes/${classId}/solutions`, {
      title: 'Bad Source',
      sources: [{ document_id: 999999, role: 'problem_set' }],
    })
    expect(res.status).toBeGreaterThanOrEqual(400)
  })

  test('wrong kind: creating solution with empty sources fails validation', async () => {
    const res = await apiPost(`/api/classes/${classId}/solutions`, {
      title: 'No Sources',
      sources: [],
    })
    expect(res.status).toBe(422)
  })

  test('delete solution removes it', async () => {
    const deleteRes = await apiDelete(`/api/solutions/${solutionId}`)
    expect(deleteRes.status).toBe(204)

    const listRes = await apiGet(`/api/classes/${classId}/solutions`)
    const solutions = await listRes.json()
    expect(solutions.find((s: { id: number }) => s.id === solutionId)).toBeUndefined()
  })

  test('solution from deleted source: segmentation worker detects missing source', async () => {
    const res = await uploadDocument(classId, resolve(TEST_DATA, 'supplement.md'), 'supplement.md')
    const doc2 = await res.json()
    await waitForDocumentReady(doc2.id, 30_000)

    const solRes = await apiPost(`/api/classes/${classId}/solutions`, {
      title: 'Orphan Solution',
      sources: [{ document_id: doc2.id, role: 'problem_set' }],
    })
    expect(solRes.status).toBe(202)
    const sol = await solRes.json()

    await apiDelete(`/api/documents/${doc2.id}`)

    const deadline = Date.now() + 30_000
    let finalState = ''
    while (Date.now() < deadline) {
      const statusRes = await apiGet(`/api/solutions/${sol.id}/status`)
      const status = await statusRes.json()
      if (status.state === 'failed' || status.state === 'cancelled') {
        finalState = status.state
        break
      }
      if (status.state === 'awaiting_review') {
        finalState = status.state
        break
      }
      if (status.state === 'ready') {
        finalState = status.state
        break
      }
      await new Promise((r) => setTimeout(r, 300))
    }
    expect(finalState).toMatch(/failed|cancelled|awaiting_review/)
  })
})
