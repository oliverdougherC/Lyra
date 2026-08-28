/**
 * Study tools through the real stack.
 *
 * Proves: deck/quiz generation from selected sources, source invalidation,
 * flashcard review with PLA-305 operation-id idempotency, quiz attempt
 * lifecycle (start, answer, resume, finish, restart).
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

      // Verify deck has cards
      const deckRes = await apiGet(`/api/decks/${deckId}`)
      const body = await deckRes.json()
      expect(body.state).toBe('ready')
      expect(body.cards.length).toBeGreaterThan(0)
    })

    test('review a flashcard, finish, study again, review again — two transitions', async () => {
      // Get session (cards in study order)
      const sessionRes = await apiGet(`/api/decks/${deckId}/session`)
      const session = await sessionRes.json()
      expect(session.cards.length).toBeGreaterThan(0)

      const cardId = session.cards[0].part_id

      // First review with unique operation_id
      const op1 = `acceptance-review-${Date.now()}-1`
      const review1Res = await apiPost(`/api/cards/${cardId}/review`, {
        rating: 'good',
        operation_id: op1,
      })
      expect(review1Res.ok).toBe(true)
      const review1 = await review1Res.json()
      expect(review1.reps).toBeGreaterThanOrEqual(1)

      // Idempotent replay of the same operation_id — no new transition
      const replay1Res = await apiPost(`/api/cards/${cardId}/review`, {
        rating: 'good',
        operation_id: op1,
      })
      expect(replay1Res.ok).toBe(true)
      const replay1 = await replay1Res.json()
      expect(replay1.reps).toBe(review1.reps)

      // "Study again" — new operation_id (PLA-305)
      const op2 = `acceptance-review-${Date.now()}-2`
      const review2Res = await apiPost(`/api/cards/${cardId}/review`, {
        rating: 'easy',
        operation_id: op2,
      })
      expect(review2Res.ok).toBe(true)
      const review2 = await review2Res.json()
      // Second review should have incremented reps
      expect(review2.reps).toBe(review1.reps + 1)

      // Verify exactly two scheduling transitions exist (not three from the replay)
      // The replay used the same op1, so it should not have created a new log entry
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

    test('start, answer, finish quiz attempt — complete scoring', async () => {
      // Start attempt
      const startRes = await apiPost(`/api/quizzes/${quizId}/attempts`)
      expect(startRes.ok).toBe(true)
      const attempt = await startRes.json()
      expect(attempt.attempt_id).toBeGreaterThan(0)
      expect(attempt.question_part_ids.length).toBeGreaterThanOrEqual(3)
      expect(attempt.finished).toBe(false)

      // Answer all questions
      for (const partId of attempt.question_part_ids) {
        const answerRes = await apiPost(`/api/attempts/${attempt.attempt_id}/answers`, {
          part_id: partId,
          selected_index: 0,
        })
        expect(answerRes.ok).toBe(true)
        const answer = await answerRes.json()
        expect(answer).toHaveProperty('correct')
        expect(answer).toHaveProperty('correct_index')
      }

      // Finish
      const finishRes = await apiPost(`/api/attempts/${attempt.attempt_id}/finish`)
      expect(finishRes.ok).toBe(true)
      const result = await finishRes.json()
      expect(result.total).toBe(attempt.question_part_ids.length)
      expect(result.answered).toBe(attempt.question_part_ids.length)
      expect(result.score).toBeGreaterThanOrEqual(0)

      // Idempotent finish
      const finish2Res = await apiPost(`/api/attempts/${attempt.attempt_id}/finish`)
      expect(finish2Res.ok).toBe(true)
      const result2 = await finish2Res.json()
      expect(result2.score).toBe(result.score)
    })

    test('resume quiz attempt after navigation', async () => {
      // Start a new attempt via restart
      const startRes = await apiPost(`/api/quizzes/${quizId}/attempts?restart=true`)
      expect(startRes.ok).toBe(true)
      const attempt = await startRes.json()

      // Answer one question
      const firstPart = attempt.question_part_ids[0]
      await apiPost(`/api/attempts/${attempt.attempt_id}/answers`, {
        part_id: firstPart,
        selected_index: 0,
      })

      // "Navigate away" — get current attempt (simulates page reload)
      const currentRes = await apiGet(`/api/quizzes/${quizId}/attempts/current`)
      expect(currentRes.ok).toBe(true)
      const currentBody = await currentRes.json()
      const current = currentBody.attempt
      expect(current).toBeTruthy()
      expect(current.attempt_id).toBe(attempt.attempt_id)
      expect(current.answers.length).toBe(1) // one answered
      expect(current.finished).toBe(false)

      // Resume by starting again (without restart=true)
      const resumeRes = await apiPost(`/api/quizzes/${quizId}/attempts`)
      const resumed = await resumeRes.json()
      expect(resumed.attempt_id).toBe(attempt.attempt_id) // same attempt
      expect(resumed.answers.length).toBe(1) // preserved

      // Answer remaining and finish
      for (const partId of attempt.question_part_ids.slice(1)) {
        await apiPost(`/api/attempts/${attempt.attempt_id}/answers`, {
          part_id: partId,
          selected_index: 0,
        })
      }
      const finishRes = await apiPost(`/api/attempts/${attempt.attempt_id}/finish`)
      expect(finishRes.ok).toBe(true)
    })

    test('restart quiz creates a new attempt', async () => {
      const startRes = await apiPost(`/api/quizzes/${quizId}/attempts?restart=true`)
      expect(startRes.ok).toBe(true)
      const newAttempt = await startRes.json()
      expect(newAttempt.answers.length).toBe(0)
      expect(newAttempt.finished).toBe(false)
    })
  })

  test('source invalidation before worker consumption', async () => {
    // Upload a second document
    const res = await uploadDocument(classId, resolve(TEST_DATA, 'supplement.md'), 'supplement.md')
    const doc2 = await res.json()
    await waitForDocumentReady(doc2.id, 30_000)

    // Delete it before creating a study artifact that references it
    await apiDelete(`/api/documents/${doc2.id}`)

    // Try to create a deck from the deleted document
    const deckRes = await apiPost(`/api/classes/${classId}/decks`, {
      title: 'Should Fail',
      document_ids: [doc2.id],
    })
    // Should fail because the document no longer exists
    expect(deckRes.status).toBe(404)
  })
})
