import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { QuizRunner } from '@/components/study/quiz-runner'
import { api } from '@/lib/api'
import type { QuizDetail, QuizQuestion, QuizQuestionRead } from '@/types'

vi.mock('next/navigation', () => ({
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

function mockAttemptLifecycle(quiz: QuizDetail) {
  vi.spyOn(api, 'getQuiz').mockResolvedValue(quiz)
  vi.spyOn(api, 'startAttempt').mockResolvedValue({
    attempt_id: 10,
    question_part_ids: quiz.questions.map((entry) => entry.part_id),
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
      name: 'Go over this with Lyra in a new tab. Your quiz stays open here.',
    })
    const href = link.getAttribute('href') ?? ''
    expect(href).toContain('/classes/1/chat?session=new&ask=')
    expect(href).not.toContain('send=1')
    const ask = new URLSearchParams(href.split('?')[1]).get('ask') ?? ''
    expect(ask).toContain('What is the determinant of the identity matrix?')
    expect(ask).toContain('"Two"')
    expect(ask).toContain('"One"')

    // Regression: an attempt in progress cannot be resumed once this tab navigates away,
    // so the handoff must open elsewhere and leave the quiz exactly where it is.
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener')

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
})
