/**
 * Study tools through the real stack.
 *
 * Proves: deck/quiz generation from selected sources, PLA-305 operation-id
 * idempotency through the real browser card-review lifecycle, source
 * invalidation AFTER study creation (PLA-291 worker validation window),
 * quiz attempt lifecycle with real browser interactions, resume after
 * navigation, and finished-attempt contract.
 */

import { test, expect } from '@playwright/test'
import { resolve } from 'node:path'
import {
  apiGet,
  apiPost,
  apiDelete,
  createClass,
  uploadDocument,
  waitForDocumentReady,
  waitForStudyReady,
  clearTutorState,
  setTutorMode,
  waitForBarrier,
  releaseBarrier,
  enableSourceBarrier,
  waitForSourceBarrier,
  releaseSourceBarrier,
  flipCard,
  rateCard,
  clickStudyAgain,
  waitForSessionSummary,
  getCardPosition,
  answerQuizQuestion,
  advanceQuiz,
  waitForQuizResults,
} from './helpers'

const TEST_DATA = resolve(__dirname, 'test-data')

test.describe('Study tools', () => {
  let classId: number
  let docId: number

  test.beforeAll(async () => {
    const cls = await createClass('Acceptance: Study')
    classId = cls.id

    const res = await uploadDocument(classId, resolve(TEST_DATA, 'sample.txt'), 'sample.txt')
    const doc = await res.json()
    docId = doc.id
    await waitForDocumentReady(docId, 30_000)
  })

  test.afterEach(async () => {
    await clearTutorState()
  })

  test.describe('Flashcard decks', () => {
    let deckId: number

    test('create deck from selected sources and wait for generation', async () => {
      const res = await apiPost(`/api/classes/${classId}/decks`, {
        title: 'Thermo Deck',
        document_ids: [docId],
        cards_per_topic: 2,
      })
      expect(res.status).toBe(202)
      const deck = await res.json()
      deckId = deck.id
      expect(deck.state).toMatch(/pending|generating/)

      await waitForStudyReady('decks', deckId, 30_000)

      const deckRes = await apiGet(`/api/decks/${deckId}`)
      const body = await deckRes.json()
      expect(body.state).toBe('ready')
      expect(body.cards.length).toBeGreaterThan(0)
    })

    test('PLA-305: browser card review, finish, Study again -- two scheduling transitions', async ({
      page,
    }) => {
      await page.goto(`/classes/${classId}/study/${deckId}`)
      await page.waitForLoadState('networkidle')

      await expect(page.getByText(/Card 1 of/)).toBeVisible({ timeout: 10_000 })

      const posText = await getCardPosition(page)
      const totalMatch = posText.match(/of (\d+)/)
      const total = totalMatch ? parseInt(totalMatch[1]) : 1

      for (let i = 0; i < total; i++) {
        await flipCard(page)
        await rateCard(page, 'Good')
        if (i < total - 1) {
          await expect(page.getByText(`Card ${i + 2} of ${total}`)).toBeVisible({ timeout: 5_000 })
        }
      }

      await waitForSessionSummary(page)
      await expect(page.getByText(/Session complete/)).toBeVisible()

      const sessionRes = await apiGet(`/api/decks/${deckId}/session`)
      const session = await sessionRes.json()
      for (const card of session.cards) {
        expect(card.card_state.reps).toBe(1)
      }

      await clickStudyAgain(page)
      await expect(page.getByText(/Card \d+ of/)).toBeVisible({ timeout: 10_000 })

      const pos2Text = await getCardPosition(page)
      const total2Match = pos2Text.match(/of (\d+)/)
      const total2 = total2Match ? parseInt(total2Match[1]) : 1
      for (let i = 0; i < total2; i++) {
        await flipCard(page)
        await rateCard(page, 'Easy')
        if (i < total2 - 1) {
          await expect(page.getByText(`Card ${i + 2} of ${total2}`)).toBeVisible({ timeout: 5_000 })
        }
      }

      await waitForSessionSummary(page)

      const session2Res = await apiGet(`/api/decks/${deckId}/session`)
      const session2 = await session2Res.json()
      for (const card of session2.cards) {
        expect(card.card_state.reps).toBe(2)
      }
    })

    test('PLA-305: API-level idempotent replay with same operation_id', async () => {
      const sessionRes = await apiGet(`/api/decks/${deckId}/session`)
      const session = await sessionRes.json()
      const cardId = session.cards[0].part_id

      const op = `acceptance-idempotent-${Date.now()}`
      const review1 = await apiPost(`/api/cards/${cardId}/review`, {
        rating: 'good',
        operation_id: op,
      })
      expect(review1.ok).toBe(true)
      const r1 = await review1.json()

      const replay = await apiPost(`/api/cards/${cardId}/review`, {
        rating: 'good',
        operation_id: op,
      })
      expect(replay.ok).toBe(true)
      const r2 = await replay.json()
      expect(r2.reps).toBe(r1.reps)
    })
  })

  test.describe('Quizzes', () => {
    let quizId: number

    test('create quiz from selected sources', async () => {
      const res = await apiPost(`/api/classes/${classId}/quizzes`, {
        title: 'Thermo Quiz',
        document_ids: [docId],
        count: 3,
        difficulty: 'basic',
        types: ['mcq'],
      })
      expect(res.status).toBe(202)
      const quiz = await res.json()
      quizId = quiz.id

      await waitForStudyReady('quizzes', quizId, 30_000)

      const quizRes = await apiGet(`/api/quizzes/${quizId}`)
      const body = await quizRes.json()
      expect(body.state).toBe('ready')
      expect(body.questions.length).toBeGreaterThanOrEqual(3)
    })

    test('browser: answer all questions and see results', async ({ page }) => {
      const startRes = await apiPost(`/api/quizzes/${quizId}/attempts?restart=true`)
      const startData = await startRes.json()
      expect(startRes.ok).toBeTruthy()
      const attemptId: number = startData.attempt_id
      expect(attemptId).toBeGreaterThan(0)

      await page.goto(`/classes/${classId}/study/${quizId}`)
      await page.waitForLoadState('networkidle')

      await expect(page.getByText(/Question 1 of/)).toBeVisible({ timeout: 10_000 })

      for (let i = 0; i < 3; i++) {
        await answerQuizQuestion(page, 0)
        await advanceQuiz(page)
      }

      await waitForQuizResults(page)

      const finishRes = await apiPost(`/api/attempts/${attemptId}/finish`)
      expect(finishRes.ok).toBe(true)
      const result = await finishRes.json()
      expect(result.total).toBe(3)
      expect(typeof result.score).toBe('number')

      const currentRes = await apiGet(`/api/quizzes/${quizId}/attempts/current`)
      const current = await currentRes.json()
      expect(current.attempt).toBeNull()
    })

    test('browser: resume quiz attempt after navigation', async ({ page }) => {
      const startRes = await apiPost(`/api/quizzes/${quizId}/attempts?restart=true`)
      const startData = await startRes.json()
      expect(startRes.ok).toBeTruthy()
      const attemptId: number = startData.attempt_id
      expect(attemptId).toBeGreaterThan(0)

      await page.goto(`/classes/${classId}/study/${quizId}`)
      await page.waitForLoadState('networkidle')
      await expect(page.getByText(/Question 1 of/)).toBeVisible({ timeout: 10_000 })

      await answerQuizQuestion(page, 0)
      await advanceQuiz(page)

      await expect(page.getByText(/Question 2 of/)).toBeVisible({ timeout: 5_000 })

      await page.goto('/')
      await page.waitForLoadState('networkidle')

      await page.goto(`/classes/${classId}/study/${quizId}`)
      await page.waitForLoadState('networkidle')

      await expect(page.getByText(/Question 2 of/)).toBeVisible({ timeout: 10_000 })

      await answerQuizQuestion(page, 0)
      await advanceQuiz(page)
      await expect(page.getByText(/Question 3 of/)).toBeVisible({ timeout: 5_000 })
      await answerQuizQuestion(page, 0)
      await advanceQuiz(page)

      await waitForQuizResults(page)

      const finishRes = await apiPost(`/api/attempts/${attemptId}/finish`)
      expect(finishRes.ok).toBe(true)
      const result = await finishRes.json()
      expect(result.total).toBe(3)

      const currentRes = await apiGet(`/api/quizzes/${quizId}/attempts/current`)
      const current = await currentRes.json()
      expect(current.attempt).toBeNull()

      const tryAgainBtn = page.getByRole('button', { name: /Try again/i })
      await expect(tryAgainBtn).toBeVisible({ timeout: 5_000 })
      await tryAgainBtn.click()

      await expect(page.getByText(/Question 1 of/)).toBeVisible({ timeout: 10_000 })
    })
  })

  test('PLA-291: source invalidation AFTER study creation -- deterministic worker barrier', async () => {
    const res = await uploadDocument(classId, resolve(TEST_DATA, 'supplement.md'), 'supplement.md')
    const doc2 = await res.json()
    await waitForDocumentReady(doc2.id, 30_000)

    // Enable the worker-level source-validation barrier so the worker
    // pauses BEFORE _validate_sources runs -- this is the exact window
    // where a source can be deleted between job creation and validation.
    await enableSourceBarrier()
    await setTutorMode('success')

    const deckRes = await apiPost(`/api/classes/${classId}/decks`, {
      title: 'Source Invalidation Test',
      document_ids: [doc2.id],
      cards_per_topic: 2,
    })
    expect(deckRes.status).toBe(202)
    const deck = await deckRes.json()

    // Wait for the worker to arrive at the barrier (before _validate_sources)
    await waitForSourceBarrier(15_000)

    // Delete the source document while the worker is paused
    await apiDelete(`/api/documents/${doc2.id}`)

    // Release the barrier -- _validate_sources will now find the source missing
    await releaseSourceBarrier()

    const deadline = Date.now() + 30_000
    let finalState = ''
    while (Date.now() < deadline) {
      const statusRes = await apiGet(`/api/decks/${deck.id}/status`)
      const status = await statusRes.json()
      if (status.state === 'failed' || status.state === 'cancelled') {
        finalState = status.state
        break
      }
      if (status.state === 'ready') {
        finalState = status.state
        break
      }
      await new Promise((r) => setTimeout(r, 300))
    }
    expect(finalState).toMatch(/failed|cancelled/)
  })
})
