import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { QuizRunner } from '@/components/study/quiz-runner'
import { api } from '@/lib/api'
import type { AttemptAnswer, QuizDetail, QuizQuestion, QuizQuestionRead } from '@/types'

vi.mock('@/router/hooks', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), prefetch: vi.fn() }),
  useParams: () => ({ id: '1', artifactId: '9' }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => '/classes/1/study/9',
}))

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { wrapper }
}

function question(
  partId: number,
  payload: Partial<QuizQuestion> & Pick<QuizQuestion, 'type' | 'question'>,
): QuizQuestionRead {
  return {
    part_id: partId,
    ordinal: partId,
    label: null,
    question: {
      options: [],
      correct_index: 0,
      explanation: 'Because the definition says so.',
      topic: 'Algebra',
      difficulty: 'intermediate',
      ...payload,
    },
  }
}

function quizWith(questions: QuizQuestionRead[]): QuizDetail {
  return {
    id: 9,
    class_id: 1,
    kind: 'quiz',
    title: 'Week 4 quiz',
    state: 'ready',
    stage_detail: null,
    problems_total: questions.length,
    problems_done: questions.length,
    error_message: null,
    created_at: '2026-08-05 09:00:00',
    updated_at: '2026-08-05 09:00:00',
    questions,
  }
}

const MCQ = question(21, {
  type: 'mcq',
  question: 'What is the determinant of the identity matrix?',
  options: ['Zero', 'One', 'Two', 'Undefined'],
  correct_index: 1,
})

const FILL_BLANK = question(22, {
  type: 'fill_blank',
  question: 'The capital of France is ...',
  options: ['Paris'],
  correct_index: 0,
})

/** Where the right answer sits for each fixture question, as the server would grade it. */
const CORRECT_INDEX: Record<number, number> = { 21: 1, 22: 0 }

function mockAttemptLifecycle(quiz: QuizDetail, answers: AttemptAnswer[] = []) {
  vi.spyOn(api, 'getQuiz').mockResolvedValue(quiz)
  vi.spyOn(api, 'startAttempt').mockResolvedValue({
    attempt_id: 10,
    question_part_ids: quiz.questions.map((entry) => entry.part_id),
    question_count: quiz.questions.length,
    answers,
    finished: false,
  })
  vi.spyOn(api, 'submitAnswer').mockImplementation((_attemptId, body) => {
    const correctIndex = CORRECT_INDEX[body.part_id] ?? 0
    return Promise.resolve({
      correct: body.selected_index === correctIndex,
      correct_index: correctIndex,
      explanation: 'Because the definition says so.',
    })
  })
}

beforeEach(() => {
  sessionStorage.clear()
  vi.restoreAllMocks()
})

describe('QuizRunner', () => {
  it('grades an option as soon as it is chosen and explains the reveal', async () => {
    mockAttemptLifecycle(quizWith([MCQ]))
    const { wrapper } = createWrapper()
    render(<QuizRunner classId={1} quizId={9} />, { wrapper })

    expect(
      await screen.findByText('What is the determinant of the identity matrix?'),
    ).toBeInTheDocument()
    expect(screen.getByText('Question 1 of 1')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'One' }))

    await waitFor(() =>
      expect(api.submitAnswer).toHaveBeenCalledWith(10, { part_id: 21, selected_index: 1 }),
    )
    expect(await screen.findByText('Correct.')).toBeInTheDocument()
    expect(screen.getByText('Because the definition says so.')).toBeInTheDocument()
    // The last question's reveal ends the attempt rather than offering a next question.
    expect(screen.getByRole('button', { name: 'See results' })).toBeInTheDocument()
  })

  it('marks a wrong option and names the right one', async () => {
    mockAttemptLifecycle(quizWith([MCQ]))
    const { wrapper } = createWrapper()
    render(<QuizRunner classId={1} quizId={9} />, { wrapper })

    await screen.findByText('What is the determinant of the identity matrix?')
    await userEvent.click(screen.getByRole('button', { name: 'Two' }))

    await waitFor(() =>
      expect(api.submitAnswer).toHaveBeenCalledWith(10, { part_id: 21, selected_index: 2 }),
    )
    expect(await screen.findByText('Not quite.')).toBeInTheDocument()
    expect(screen.getByText('The answer:')).toBeInTheDocument()
  })

  it('offers to take a miss to the tutor without costing the attempt in progress', async () => {
    mockAttemptLifecycle(quizWith([MCQ]))
    const { wrapper } = createWrapper()
    render(<QuizRunner classId={1} quizId={9} />, { wrapper })

    await screen.findByText('What is the determinant of the identity matrix?')
    await userEvent.click(screen.getByRole('button', { name: 'Two' }))
    await screen.findByText('Not quite.')

    // The link opens a fresh conversation with the question prefilled, not sent: the
    // words are generated, so the student sees them in the composer before they go.
    const link = screen.getByRole('link', {
      name: 'Go over this with Lyra',
    })
    const href = link.getAttribute('href') ?? ''
    expect(href).toContain('/#/classes/1/chat?session=new&ask=')
    expect(link).not.toHaveAttribute('target', '_blank')
    expect(href).not.toContain('send=1')
    const ask = new URLSearchParams(href.split('?')[1]).get('ask') ?? ''
    expect(ask).toContain('What is the determinant of the identity matrix?')
    expect(ask).toContain('"Two"')
    expect(ask).toContain('"One"')

    await userEvent.click(link)
    expect(JSON.parse(sessionStorage.getItem('lyra:quiz:9:help-return') ?? 'null')).toEqual({
      attemptId: 10,
      partId: 21,
    })

    // The attempt itself was not restarted or abandoned by rendering the link.
    expect(api.startAttempt).toHaveBeenCalledTimes(1)
  })

  it('maps a matching fill_blank answer to selected_index 0', async () => {
    mockAttemptLifecycle(quizWith([FILL_BLANK]))
    const { wrapper } = createWrapper()
    render(<QuizRunner classId={1} quizId={9} />, { wrapper })

    await screen.findByText('The capital of France is ...')
    // Case and surrounding whitespace do not count against the student.
    await userEvent.type(screen.getByLabelText('Your answer'), '  paris ')
    await userEvent.click(screen.getByRole('button', { name: 'Check' }))

    await waitFor(() =>
      expect(api.submitAnswer).toHaveBeenCalledWith(10, { part_id: 22, selected_index: 0 }),
    )
  })

  it('maps a missing fill_blank answer to selected_index -1', async () => {
    mockAttemptLifecycle(quizWith([FILL_BLANK]))
    const { wrapper } = createWrapper()
    render(<QuizRunner classId={1} quizId={9} />, { wrapper })

    await screen.findByText('The capital of France is ...')
    await userEvent.type(screen.getByLabelText('Your answer'), 'Lyon')
    await userEvent.click(screen.getByRole('button', { name: 'Check' }))

    await waitFor(() =>
      expect(api.submitAnswer).toHaveBeenCalledWith(10, { part_id: 22, selected_index: -1 }),
    )
  })

  it('finishes the attempt and renders the summary the server scores', async () => {
    mockAttemptLifecycle(quizWith([MCQ, FILL_BLANK]))
    vi.spyOn(api, 'finishAttempt').mockResolvedValue({
      score: 1,
      total: 2,
      answered: 2,
      by_topic: [
        { topic: 'Algebra', correct: 1, total: 1 },
        { topic: 'Geography', correct: 0, total: 1 },
      ],
    })
    const { wrapper } = createWrapper()
    render(<QuizRunner classId={1} quizId={9} />, { wrapper })

    await screen.findByText('What is the determinant of the identity matrix?')
    await userEvent.click(screen.getByRole('button', { name: 'One' }))
    await screen.findByText('Correct.')

    await userEvent.click(screen.getByRole('button', { name: 'Next' }))

    await screen.findByText('The capital of France is ...')
    await userEvent.type(screen.getByLabelText('Your answer'), 'Lyon')
    await userEvent.click(screen.getByRole('button', { name: 'Check' }))
    await screen.findByText('Not quite.')

    await userEvent.click(screen.getByRole('button', { name: 'See results' }))

    await waitFor(() => expect(api.finishAttempt).toHaveBeenCalledWith(10))
    expect(await screen.findByText('You scored 1 out of 2')).toBeInTheDocument()
    expect(screen.getByLabelText('Algebra: 1 of 1 correct')).toBeInTheDocument()
    expect(screen.getByLabelText('Geography: 0 of 1 correct')).toBeInTheDocument()

    // The weak-topic handoff may navigate this tab: the attempt is finished and scored,
    // so there is nothing left here to lose.
    const weakLink = screen.getByRole('link', { name: /Go over this with Lyra/ })
    expect(weakLink).not.toHaveAttribute('target')
  })

  it('resumes at the first unanswered question when the attempt already has answers', async () => {
    // The active attempt already recorded an answer to the first question, so a reload
    // resumes at the second rather than starting over (PLA-277).
    mockAttemptLifecycle(quizWith([MCQ, FILL_BLANK]), [
      { part_id: 21, selected_index: 1, correct: true },
    ])
    const { wrapper } = createWrapper()
    render(<QuizRunner classId={1} quizId={9} />, { wrapper })

    expect(await screen.findByText('The capital of France is ...')).toBeInTheDocument()
    expect(screen.getByText('Question 2 of 2')).toBeInTheDocument()
  })

  it('lets a fully answered resumed attempt finish without re-answering the last question', async () => {
    mockAttemptLifecycle(quizWith([MCQ, FILL_BLANK]), [
      { part_id: 21, selected_index: 1, correct: true },
      { part_id: 22, selected_index: 0, correct: true },
    ])
    vi.spyOn(api, 'finishAttempt').mockResolvedValue({
      score: 2,
      total: 2,
      answered: 2,
      by_topic: [{ topic: 'Algebra', correct: 2, total: 2 }],
    })
    const { wrapper } = createWrapper()
    render(<QuizRunner classId={1} quizId={9} />, { wrapper })

    expect(await screen.findByText('The capital of France is ...')).toBeInTheDocument()
    expect(screen.getByText('Question 2 of 2')).toBeInTheDocument()
    expect(await screen.findByText('Correct.')).toBeInTheDocument()
    expect(api.submitAnswer).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: 'See results' }))

    await waitFor(() => expect(api.finishAttempt).toHaveBeenCalledWith(10))
    expect(await screen.findByText('You scored 2 out of 2')).toBeInTheDocument()
  })

  it('starts over explicitly, opening a fresh attempt', async () => {
    mockAttemptLifecycle(quizWith([MCQ, FILL_BLANK]))
    const { wrapper } = createWrapper()
    render(<QuizRunner classId={1} quizId={9} />, { wrapper })

    await screen.findByText('What is the determinant of the identity matrix?')
    await userEvent.click(screen.getByRole('button', { name: 'Start over' }))

    await waitFor(() => expect(api.startAttempt).toHaveBeenCalledWith(9, true))
  })
})

describe('QuizRunner remediation', () => {
  it('prevents restarting while grading or finishing and focuses feedback and results', async () => {
    mockAttemptLifecycle(quizWith([MCQ]))
    const grading = Promise.withResolvers<Awaited<ReturnType<typeof api.submitAnswer>>>()
    const scoring = Promise.withResolvers<Awaited<ReturnType<typeof api.finishAttempt>>>()
    vi.mocked(api.submitAnswer).mockReturnValue(grading.promise)
    vi.spyOn(api, 'finishAttempt').mockReturnValue(scoring.promise)
    render(<QuizRunner classId={1} quizId={9} />, { wrapper: createWrapper().wrapper })
    await userEvent.click(await screen.findByRole('button', { name: 'One' }))
    expect(screen.getByRole('button', { name: 'Start over' })).toBeDisabled()
    expect(screen.getByRole('status')).toHaveTextContent('Checking your answer')
    grading.resolve({ correct: true, correct_index: 1, explanation: 'Correct by definition.' })
    await waitFor(() =>
      expect(screen.getByRole('region', { name: 'Answer feedback' })).toHaveFocus(),
    )
    await userEvent.click(screen.getByRole('button', { name: 'See results' }))
    expect(screen.getByRole('button', { name: 'Start over' })).toBeDisabled()
    scoring.resolve({ score: 1, total: 1, answered: 1, by_topic: [] })
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'You scored 1 out of 1' })).toHaveFocus(),
    )
  })

  it('focuses the next question after advancing', async () => {
    mockAttemptLifecycle(quizWith([MCQ, FILL_BLANK]))
    render(<QuizRunner classId={1} quizId={9} />, { wrapper: createWrapper().wrapper })
    await userEvent.click(await screen.findByRole('button', { name: 'One' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Next' }))
    expect(screen.getByRole('heading', { name: 'The capital of France is ...' })).toHaveFocus()
  })
})

it('restores the recorded reveal through repeated Ask history returns without submitting again', async () => {
  mockAttemptLifecycle(quizWith([MCQ, FILL_BLANK]))
  const { wrapper } = createWrapper()
  const view = render(<QuizRunner classId={1} quizId={9} />, { wrapper })
  await userEvent.click(await screen.findByRole('button', { name: 'Two' }))
  await screen.findByText('Not quite.')
  await userEvent.click(screen.getByRole('link', { name: 'Go over this with Lyra' }))
  view.unmount()
  vi.mocked(api.startAttempt).mockResolvedValue({
    attempt_id: 10,
    question_part_ids: [21, 22],
    question_count: 2,
    answers: [{ part_id: 21, selected_index: 2, correct: false }],
    finished: false,
  })
  const returned = render(<QuizRunner classId={1} quizId={9} />, { wrapper })
  expect(await screen.findByText('Not quite.')).toBeVisible()
  expect(screen.getByText('Question 1 of 2')).toBeVisible()
  expect(api.submitAnswer).toHaveBeenCalledTimes(1)
  returned.unmount()
  render(<QuizRunner classId={1} quizId={9} />, { wrapper })
  expect(await screen.findByText('Not quite.')).toBeVisible()
  expect(screen.getByText('Question 1 of 2')).toBeVisible()
  expect(api.submitAnswer).toHaveBeenCalledTimes(1)
  await userEvent.click(screen.getByRole('button', { name: 'Next' }))
  expect(screen.getByText('The capital of France is ...')).toBeVisible()
  expect(api.submitAnswer).toHaveBeenCalledTimes(1)
  expect(sessionStorage.getItem('lyra:quiz:9:help-return')).toBeNull()
})

it('ignores help return state from a different attempt', async () => {
  sessionStorage.setItem('lyra:quiz:9:help-return', JSON.stringify({ attemptId: 8, partId: 21 }))
  mockAttemptLifecycle(quizWith([MCQ, FILL_BLANK]), [
    { part_id: 21, selected_index: 2, correct: false },
  ])
  render(<QuizRunner classId={1} quizId={9} />, { wrapper: createWrapper().wrapper })
  expect(await screen.findByText('The capital of France is ...')).toBeVisible()
  expect(screen.queryByText('Not quite.')).not.toBeInTheDocument()
  expect(sessionStorage.getItem('lyra:quiz:9:help-return')).toBeNull()
  expect(api.submitAnswer).not.toHaveBeenCalled()
})

it('clears help return state when starting over', async () => {
  sessionStorage.setItem('lyra:quiz:9:help-return', JSON.stringify({ attemptId: 10, partId: 21 }))
  mockAttemptLifecycle(quizWith([MCQ, FILL_BLANK]), [
    { part_id: 21, selected_index: 2, correct: false },
  ])
  render(<QuizRunner classId={1} quizId={9} />, { wrapper: createWrapper().wrapper })
  await screen.findByText('Not quite.')
  await userEvent.click(screen.getByRole('button', { name: 'Start over' }))
  expect(sessionStorage.getItem('lyra:quiz:9:help-return')).toBeNull()
  expect(api.startAttempt).toHaveBeenLastCalledWith(9, true)
})

it('keeps the typed fill-blank answer when returning from Ask', async () => {
  mockAttemptLifecycle(quizWith([FILL_BLANK]))
  const { wrapper } = createWrapper()
  const view = render(<QuizRunner classId={1} quizId={9} />, { wrapper })
  await userEvent.type(await screen.findByRole('textbox', { name: 'Your answer' }), 'Lyon')
  await userEvent.click(screen.getByRole('button', { name: 'Check' }))
  await screen.findByText('Not quite.')
  await userEvent.click(screen.getByRole('link', { name: 'Go over this with Lyra' }))
  view.unmount()
  vi.mocked(api.startAttempt).mockResolvedValue({
    attempt_id: 10,
    question_part_ids: [22],
    question_count: 1,
    answers: [{ part_id: 22, selected_index: -1, correct: false }],
    finished: false,
  })
  render(<QuizRunner classId={1} quizId={9} />, { wrapper })
  await screen.findByText('Not quite.')
  expect(screen.getByRole('textbox', { name: 'Your answer' })).toHaveValue('Lyon')
  expect(api.submitAnswer).toHaveBeenCalledTimes(1)
})
