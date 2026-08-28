/**
 * Study tools through the real stack.
 *
 * Proves: deck/quiz generation from selected sources, PLA-305 operation-id
 * idempotency through the real browser card-review lifecycle, source
 * invalidation AFTER study creation (PLA-291 worker validation window),
 * quiz attempt lifecycle with real browser interactions.
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
      // Navigate to the deck study page in the real browser
      await page.goto(`/classes/${classId}/study/${deckId}`)
      await page.waitForLoadState('networkidle')

      // Wait for the first card to render
      await expect(page.getByText(/Card 1 of/)).toBeVisible({ timeout: 10_000 })

      // Get the card count from the position text
      const posText = await getCardPosition(page)
      const totalMatch = posText.match(/of (\d+)/)
      const total = totalMatch ? parseInt(totalMatch[1]) : 1

      // Review all cards through the browser: flip, rate
      for (let i = 0; i < total; i++) {
        await flipCard(page)
        await rateCard(page, 'Good')
        // Brief wait for the next card to render (or summary to appear)
        if (i < total - 1) {
          await expect(page.getByText(`Card ${i + 2} of ${total}`)).toBeVisible({ timeout: 5_000 })
        }
      }

      // Session summary should appear
      await waitForSessionSummary(page)
      await expect(page.getByText(/Session complete/)).toBeVisible()

      // PLA-305: exactly `total` scheduling transitions, not "at least"
      const sessionRes = await apiGet(`/api/decks/${deckId}/session`)
      const session = await sessionRes.json()
      for (const card of session.cards) {
        expect(card.card_state.reps).toBe(1)
      }

      // Click "Study again" -- this is the PLA-305 lifecycle: the frontend
      // generates a NEW operation_id for the new review cycle
      await clickStudyAgain(page)

      // Cards should reappear (at least one, depending on scheduling)
      await expect(page.getByText(/Card \d+ of/)).toBeVisible({ timeout: 10_000 })

      // Review cards in the second pass
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

      // PLA-305: exactly 2 reviews total (one per pass), not "at least 2"
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

      // Replay same operation_id -- must not create a new transition
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
      // Start a fresh attempt via API so we have a clean state
      await apiPost(`/api/quizzes/${quizId}/attempts?restart=true`)

      await page.goto(`/classes/${classId}/study/${quizId}`)
      await page.waitForLoadState('networkidle')

      // Wait for the first question to render (MCQ options are buttons in a list)
      await expect(page.getByText(/Question 1 of/)).toBeVisible({ timeout: 10_000 })

      // Answer 3 questions through the browser
      for (let i = 0; i < 3; i++) {
        await answerQuizQuestion(page, 0)
        await advanceQuiz(page)
      }

      // Results should appear
      await waitForQuizResults(page)

      // Verify via API that the attempt is finished with correct scoring
      const currentRes = await apiGet(`/api/quizzes/${quizId}/attempts/current`)
      const current = await currentRes.json()
      expect(current.attempt?.finished).toBe(true)
      expect(current.attempt?.answers.length).toBe(3)
    })

    test('browser: resume quiz attempt after navigation', async ({ page }) => {
      // Start a fresh attempt via API
      await apiPost(`/api/quizzes/${quizId}/attempts?restart=true`)

      // Open quiz in browser
      await page.goto(`/classes/${classId}/study/${quizId}`)
      await page.waitForLoadState('networkidle')
      await expect(page.getByText(/Question 1 of/)).toBeVisible({ timeout: 10_000 })

      // Answer one question
      await answerQuizQuestion(page, 0)
      await advanceQuiz(page)

      // Wait for question 2 to render
      await expect(page.getByText(/Question 2 of/)).toBeVisible({ timeout: 5_000 })

      // Navigate away
      await page.goto('/')
      await page.waitForLoadState('networkidle')

      // Return to the quiz
      await page.goto(`/classes/${classId}/study/${quizId}`)
      await page.waitForLoadState('networkidle')

      // Should resume at question 2 (same attempt, answer preserved)
      await expect(page.getByText(/Question 2 of/)).toBeVisible({ timeout: 10_000 })

      // Answer remaining questions and finish
      await answerQuizQuestion(page, 0)
      await advanceQuiz(page)
      await expect(page.getByText(/Question 3 of/)).toBeVisible({ timeout: 5_000 })
      await answerQuizQuestion(page, 0)
      await advanceQuiz(page)

      // Results should appear
      await waitForQuizResults(page)

      // Verify via API that the attempt is finished
      const currentRes = await apiGet(`/api/quizzes/${quizId}/attempts/current`)
      const current = await currentRes.json()
      expect(current.attempt?.finished).toBe(true)
      expect(current.attempt?.answers.length).toBe(3)

      // "Start over" (Try again on results) produces a new attempt
      const tryAgainBtn = page.getByRole('button', { name: /Try again/i })
      await expect(tryAgainBtn).toBeVisible({ timeout: 5_000 })
      await tryAgainBtn.click()

      // New attempt starts at question 1
      await expect(page.getByText(/Question 1 of/)).toBeVisible({ timeout: 10_000 })
    })
  })

  test('PLA-291: source invalidation AFTER study creation -- worker validates sources', async () => {
    // Upload a second document and wait for it to be ready
    const res = await uploadDocument(classId, resolve(TEST_DATA, 'supplement.md'), 'supplement.md')
    const doc2 = await res.json()
    await waitForDocumentReady(doc2.id, 30_000)

    // Create the study artifact FIRST (referencing the document that still exists)
    const deckRes = await apiPost(`/api/classes/${classId}/decks`, {
      title: 'Source Invalidation Test',
      document_ids: [doc2.id],
      cards_per_topic: 2,
    })
    expect(deckRes.status).toBe(202)
    const deck = await deckRes.json()

    // NOW delete the source document -- this is the PLA-291 window: the worker
    // hasn't consumed the job yet (or will validate sources before generation)
    await apiDelete(`/api/documents/${doc2.id}`)

    // The deck should reach failed state because its source no longer exists
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
    // The worker should have detected the missing source and failed the job
    expect(finalState).toMatch(/failed|cancelled/)
  })
})
