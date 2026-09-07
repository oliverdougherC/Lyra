/** PR #80's real attempt metadata drives PR #82's Practice continuation UI. */
import { test, expect } from '@playwright/test'
import { resolve } from 'node:path'
import {
  advanceQuiz,
  answerQuizQuestion,
  apiGet,
  apiPost,
  clearTutorState,
  createClass,
  uploadDocument,
  waitForDocumentReady,
  waitForQuizResults,
  waitForStudyReady,
} from './helpers'

type Attempt = {
  attempt_id: number
  question_part_ids: number[]
  question_count: number
  answers: { part_id: number; selected_index: number }[]
}
type ListedQuiz = { id: number; active_attempt_id: number | null; answered_count: number }

test.describe('Integrated quiz continuation', () => {
  let classId: number
  let documentId: number

  test.beforeAll(async () => {
    await clearTutorState()
    classId = (await createClass('Acceptance: integrated quiz continuation')).id
    const uploaded = await uploadDocument(
      classId,
      resolve(__dirname, 'test-data/sample.txt'),
      'sample.txt',
    )
    expect(uploaded.ok).toBe(true)
    documentId = (await uploaded.json()).id
    await waitForDocumentReady(documentId)
  })
  test.afterEach(clearTutorState)

  async function createAnsweredQuiz(title: string) {
    const created = await apiPost(`/api/classes/${classId}/quizzes`, {
      title,
      document_ids: [documentId],
      count: 3,
      difficulty: 'basic',
      types: ['mcq'],
    })
    expect(created.status).toBe(202)
    const quizId: number = (await created.json()).id
    await waitForStudyReady('quizzes', quizId)
    const started = await apiPost(`/api/quizzes/${quizId}/attempts`)
    expect(started.ok).toBe(true)
    const attempt: Attempt = await started.json()
    expect(attempt.question_count).toBeGreaterThanOrEqual(3)
    const answered = await apiPost(`/api/attempts/${attempt.attempt_id}/answers`, {
      part_id: attempt.question_part_ids[0],
      selected_index: 0,
    })
    expect(answered.ok).toBe(true)
    return { quizId, attempt }
  }

  async function listedQuiz(quizId: number): Promise<ListedQuiz> {
    const response = await apiGet(`/api/classes/${classId}/study`)
    expect(response.ok).toBe(true)
    const quizzes: ListedQuiz[] = (await response.json()).quizzes
    const quiz = quizzes.find((item) => item.id === quizId)
    expect(quiz).toBeDefined()
    return quiz!
  }

  async function currentAttempt(quizId: number): Promise<Attempt | null> {
    const response = await apiGet(`/api/quizzes/${quizId}/attempts/current`)
    expect(response.ok).toBe(true)
    return (await response.json()).attempt
  }

  test('Continue quiz resumes the actual answered attempt through reload and navigation', async ({
    page,
  }) => {
    const { quizId, attempt } = await createAnsweredQuiz('Continue the saved quiz')
    expect(await listedQuiz(quizId)).toMatchObject({
      active_attempt_id: attempt.attempt_id,
      answered_count: 1,
    })
    await page.goto(`/classes/${classId}?tab=practice`)
    const practice = page.getByRole('tabpanel', { name: /Practice/ })
    await expect(practice.getByText(/\b1 answered\b/)).toBeVisible()
    await practice.getByRole('button', { name: 'Continue quiz', exact: true }).click()
    await expect(page).toHaveURL(new RegExp(`/classes/${classId}/study/${quizId}$`))
    await expect(
      page.getByText(`Question 2 of ${attempt.question_count}`, { exact: true }),
    ).toBeVisible()
    expect(await currentAttempt(quizId)).toMatchObject({
      attempt_id: attempt.attempt_id,
      answers: [{ part_id: attempt.question_part_ids[0], selected_index: 0 }],
    })
    await page.reload()
    await expect(
      page.getByText(`Question 2 of ${attempt.question_count}`, { exact: true }),
    ).toBeVisible()
    await page.getByRole('link', { name: 'Practice', exact: true }).click()
    await expect(practice.getByText(/\b1 answered\b/)).toBeVisible()
    await practice.getByRole('button', { name: 'Continue quiz', exact: true }).click()
    await expect(
      page.getByText(`Question 2 of ${attempt.question_count}`, { exact: true }),
    ).toBeVisible()
    expect((await currentAttempt(quizId))?.attempt_id).toBe(attempt.attempt_id)
    for (let index = 1; index < attempt.question_count; index++) {
      await expect(
        page.getByText(`Question ${index + 1} of ${attempt.question_count}`, { exact: true }),
      ).toBeVisible()
      await answerQuizQuestion(page, 0)
      await advanceQuiz(page)
      if (index === 1 && index < attempt.question_count - 1) {
        await page.getByRole('link', { name: 'Practice', exact: true }).click()
        await expect(practice.getByText(/\b2 answered\b/)).toBeVisible()
        expect(await listedQuiz(quizId)).toMatchObject({
          active_attempt_id: attempt.attempt_id,
          answered_count: 2,
        })
        await practice.getByRole('button', { name: 'Continue quiz', exact: true }).click()
        expect((await currentAttempt(quizId))?.attempt_id).toBe(attempt.attempt_id)
      }
    }
    await waitForQuizResults(page)
    expect(await currentAttempt(quizId)).toBeNull()
    expect(await listedQuiz(quizId)).toMatchObject({ active_attempt_id: null, answered_count: 0 })
    await page.getByRole('link', { name: 'Practice', exact: true }).click()
    await expect(practice.getByRole('button', { name: 'Continue quiz', exact: true })).toHaveCount(
      0,
    )
    await expect(practice.getByText(/\b\d+ answered\b/)).toHaveCount(0)
    await expect(practice.getByText('Open quiz', { exact: true })).toBeVisible()
  })

  test('abandoning through explicit restart never advertises the previous answers', async ({
    page,
  }) => {
    const { quizId, attempt } = await createAnsweredQuiz('Restart the saved quiz')
    expect(await listedQuiz(quizId)).toMatchObject({
      active_attempt_id: attempt.attempt_id,
      answered_count: 1,
    })
    // The supported abandon operation opens a fresh attempt; no DB mutation or
    // fictitious standalone abandon route is used by this acceptance test.
    const restarted = await apiPost(`/api/quizzes/${quizId}/attempts?restart=true`)
    expect(restarted.ok).toBe(true)
    const replacement: Attempt = await restarted.json()
    expect(replacement.attempt_id).not.toBe(attempt.attempt_id)
    expect(replacement.answers).toEqual([])
    expect(await listedQuiz(quizId)).toMatchObject({
      active_attempt_id: replacement.attempt_id,
      answered_count: 0,
    })
    await page.goto(`/classes/${classId}?tab=practice`)
    const practice = page.getByRole('tabpanel', { name: /Practice/ })
    await expect(practice.getByText(/\b0 answered\b/)).toBeVisible()
    await expect(practice.getByText(/\b1 answered\b/)).toHaveCount(0)
    await practice.getByRole('button', { name: 'Continue quiz', exact: true }).click()
    await expect(
      page.getByText(`Question 1 of ${replacement.question_count}`, { exact: true }),
    ).toBeVisible()
    expect((await currentAttempt(quizId))?.attempt_id).toBe(replacement.attempt_id)
    const finished = await apiPost(`/api/attempts/${replacement.attempt_id}/finish`)
    expect(finished.ok).toBe(true)
    expect(await currentAttempt(quizId)).toBeNull()
    expect(await listedQuiz(quizId)).toMatchObject({ active_attempt_id: null, answered_count: 0 })
    await page.goto(`/classes/${classId}?tab=practice`)
    await expect(practice.getByRole('button', { name: 'Continue quiz', exact: true })).toHaveCount(
      0,
    )
    await expect(practice.getByText(/\b\d+ answered\b/)).toHaveCount(0)
  })
})
