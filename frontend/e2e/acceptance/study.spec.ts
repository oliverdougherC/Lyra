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

    for (const timing of ['before commit', 'after commit'] as const) {
      test(`PLA-477: recover Easy after response failure ${timing} and reload`, async ({
        page,
      }) => {
        const created = await apiPost(`/api/classes/${classId}/decks`, {
          title: `Recovery ${timing}`,
          document_ids: [docId],
          cards_per_topic: 2,
        })
        const deck = await created.json()
        await waitForStudyReady('decks', deck.id)
        const before = await (await apiGet(`/api/decks/${deck.id}/session`)).json()
        const first = before.cards[0]
        let failedPayload: { rating: string; operation_id: string } | undefined
        let committed: { reps: number; stability: number; due_at: string } | undefined
        // Forward to the real server first in the acknowledgement-loss case. Only
        // the transport response is dropped; scheduling and logging are production code.
        await page.route(
          `**/api/cards/${first.part_id}/review`,
          async (route) => {
            failedPayload = route.request().postDataJSON()
            if (timing === 'after commit') {
              const response = await route.fetch()
              expect(response.ok()).toBe(true)
              committed = await response.json()
            }
            await route.abort('failed')
          },
          { times: 1 },
        )
        await page.goto(`/classes/${classId}/study/${deck.id}`)
        await expect(page.getByText(/Card 1 of/)).toBeVisible()
        await flipCard(page)
        await rateCard(page, 'Easy')
        await expect(page.getByText(/Easy review could not be confirmed/)).toBeVisible()
        await expect(page.getByRole('button', { name: /^Again/ })).toBeDisabled()
        await page.keyboard.press('1')
        const during = await (await apiGet(`/api/decks/${deck.id}/session`)).json()
        expect(
          during.cards.find((card: { part_id: number }) => card.part_id === first.part_id)
            .card_state.reps,
        ).toBe(timing === 'after commit' ? 1 : 0)

        await page.reload()
        await expect(page.getByText(/Easy review could not be confirmed/)).toBeVisible()
        await expect(page.getByRole('button', { name: /^Again/ })).toBeDisabled()
        await rateCard(page, 'Easy')
        await expect(page.getByText(/Card 2 of/)).toBeVisible()
        expect(failedPayload?.rating).toBe('easy')
        const replay = await apiPost(`/api/cards/${first.part_id}/review`, failedPayload)
        expect(replay.ok).toBe(true)
        const saved = await replay.json()
        expect(saved.reps).toBe(1)
        if (committed) expect(saved).toEqual(committed)
        const mismatch = await apiPost(`/api/cards/${first.part_id}/review`, {
          ...failedPayload,
          rating: 'again',
        })
        expect(mismatch.status).toBe(409)
        for (let i = 1; i < before.cards.length; i++) {
          await flipCard(page)
          await rateCard(page, 'Good')
          if (i < before.cards.length - 1) {
            await expect(page.getByText(`Card ${i + 2} of ${before.cards.length}`)).toBeVisible()
          }
        }
        await waitForSessionSummary(page)
        await page.getByText('Review details', { exact: true }).click()
        await expect(page.getByText(/0 again.*1 easy/)).toBeVisible()
        await page.reload()
        await waitForSessionSummary(page)
        await page.getByText('Review details', { exact: true }).click()
        await expect(page.getByText(/0 again.*1 easy/)).toBeVisible()
        const final = await (await apiGet(`/api/decks/${deck.id}/session`)).json()
        const finalState = final.cards.find(
          (card: { part_id: number }) => card.part_id === first.part_id,
        ).card_state
        expect(finalState.reps).toBe(1)
        expect(finalState.due_at).toBe(saved.due_at)
        expect(finalState.stability).toBe(saved.stability)
      })
    }

    test('PLA-476: 100 distinct same-day reviews remain bounded and recorded', async () => {
      const session = await (await apiGet(`/api/decks/${deckId}/session`)).json()
      const card = session.cards[0]
      const original = card.card_state
      for (let i = 0; i < 100; i++) {
        const response = await apiPost(`/api/cards/${card.part_id}/review`, {
          rating: i % 2 ? 'good' : 'easy',
          operation_id: `bounded-${deckId}-${i}`,
        })
        expect(response.ok).toBe(true)
        const updated = await response.json()
        expect(updated.reps).toBe(original.reps + i + 1)
        expect(updated.stability).toBe(original.stability)
        expect(updated.due_at).toBe(original.due_at)
        expect(Number.isFinite(Date.parse(updated.due_at))).toBe(true)
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

  for (const kind of ['decks', 'quizzes'] as const) {
    test(`PLA-472: ${kind} cancellation stays terminal after a queued worker starts`, async () => {
      await enableSourceBarrier()
      const response = await apiPost(`/api/classes/${classId}/${kind}`, {
        title: 'Cancel queued study',
        document_ids: [docId],
        ...(kind === 'decks' ? { cards_per_topic: 2 } : { count: 3, types: ['mcq'] }),
      })
      expect(response.status).toBe(202)
      const artifact = await response.json()
      await waitForSourceBarrier()
      const cancelled = await apiPost(`/api/${kind}/${artifact.id}/cancel`)
      expect(cancelled.ok).toBe(true)
      expect((await cancelled.json()).state).toBe('cancelled')
      await releaseSourceBarrier()
      const duplicate = await apiPost(`/api/${kind}/${artifact.id}/cancel`)
      expect((await duplicate.json()).state).toBe('cancelled')
      const status = await apiGet(`/api/${kind}/${artifact.id}/status`)
      expect((await status.json()).state).toBe('cancelled')
      expect((await apiGet(`/api/${kind}/${artifact.id}`)).ok).toBe(false)
    })
  }

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
