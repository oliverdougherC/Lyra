'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { classKeys } from '@/lib/hooks/use-classes'
import type {
  AnswerCreate,
  ArtifactState,
  CardUpdate,
  DeckCreate,
  QuizCreate,
  Rating,
  StudyListRead,
} from '@/types'

export const studyKeys = {
  list: (classId: number) => ['study', classId] as const,
  deck: (deckId: number) => ['deck', deckId] as const,
  quiz: (quizId: number) => ['quiz', quizId] as const,
  deckStatus: (deckId: number) => ['deck-status', deckId] as const,
  quizStatus: (quizId: number) => ['quiz-status', quizId] as const,
  deckSession: (deckId: number, limit: number) => ['deck-session', deckId, limit] as const,
}

/**
 * The states the generation worker is still on. Decks and quizzes move pending to
 * generating to ready, and polling either side of that boundary is asking the same
 * question forever.
 */
const GENERATING_STATES: readonly ArtifactState[] = ['pending', 'generating']

export function isGenerating(state: ArtifactState): boolean {
  return GENERATING_STATES.includes(state)
}

/** How often the list asks again while the worker is on anything in it. */
const IN_FLIGHT_POLL_MS = 1500

/**
 * How often to ask for the list again, given what the list currently says.
 *
 * Generation is a background job on the server: nothing is pushed, so the only way the
 * panel learns that a deck moved from `generating` to `ready` is to ask. Deriving that
 * from the data is what makes it self-healing, the same derivation the document list
 * makes for ingestion.
 */
function studyListPollInterval(list: StudyListRead | undefined): number | false {
  const inFlight =
    (list?.decks.some((deck) => isGenerating(deck.state)) ?? false) ||
    (list?.quizzes.some((quiz) => isGenerating(quiz.state)) ?? false)
  return inFlight ? IN_FLIGHT_POLL_MS : false
}

export function useStudyList(classId: number, enabled = true) {
  return useQuery({
    queryKey: studyKeys.list(classId),
    queryFn: ({ signal }) => api.listStudy(classId, signal),
    enabled: enabled && Number.isFinite(classId),
    refetchInterval: (query) => studyListPollInterval(query.state.data),
    // Generation does not pause because the student switched windows, and a backgrounded
    // tab must not freeze a run in progress.
    refetchIntervalInBackground: true,
  })
}

export function useDeck(deckId: number, enabled = true) {
  return useQuery({
    queryKey: studyKeys.deck(deckId),
    queryFn: ({ signal }) => api.getDeck(deckId, signal),
    enabled: enabled && Number.isFinite(deckId),
  })
}

export function useQuiz(quizId: number, enabled = true) {
  return useQuery({
    queryKey: studyKeys.quiz(quizId),
    queryFn: ({ signal }) => api.getQuiz(quizId, signal),
    enabled: enabled && Number.isFinite(quizId),
  })
}

/**
 * The interval both status polls share, copied from the solutions poll: 500ms at first so
 * a fresh run feels immediate, +250ms per poll, capped at 2s, and stopped the moment the
 * worker leaves `pending` or `generating`.
 */
function generatingPollInterval(query: {
  state: { data: { state: ArtifactState } | undefined; dataUpdateCount: number }
}): number | false {
  const status = query.state.data
  if (!status) return 500
  if (!isGenerating(status.state)) return false
  return Math.min(500 + query.state.dataUpdateCount * 250, 2000)
}

export function useDeckStatus(deckId: number, enabled = true) {
  return useQuery({
    queryKey: studyKeys.deckStatus(deckId),
    queryFn: ({ signal }) => api.getDeckStatus(deckId, signal),
    enabled: enabled && Number.isFinite(deckId),
    refetchInterval: generatingPollInterval,
  })
}

export function useQuizStatus(quizId: number, enabled = true) {
  return useQuery({
    queryKey: studyKeys.quizStatus(quizId),
    queryFn: ({ signal }) => api.getQuizStatus(quizId, signal),
    enabled: enabled && Number.isFinite(quizId),
    refetchInterval: generatingPollInterval,
  })
}

/** Cards in study order for one session, capped at `limit`. */
export function useDeckSession(deckId: number, limit = 20, enabled = true) {
  return useQuery({
    queryKey: studyKeys.deckSession(deckId, limit),
    queryFn: ({ signal }) => api.getDeckSession(deckId, limit, signal),
    enabled: enabled && Number.isFinite(deckId),
  })
}

/**
 * Records one rating. The session queue is deliberately not invalidated: the session
 * screen owns it for the run's duration, and refetching would reshuffle the cards under
 * the student's thumb. The deck detail is, because its card states just changed.
 */
export function useReviewCard(deckId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ partId, rating }: { partId: number; rating: Rating }) =>
      api.reviewCard(partId, rating),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: studyKeys.deck(deckId) })
    },
  })
}

export function useUpdateCard(deckId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ partId, body }: { partId: number; body: CardUpdate }) =>
      api.updateCard(partId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: studyKeys.deck(deckId) })
    },
  })
}

export function useDeleteCard(deckId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (partId: number) => api.deleteCard(partId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: studyKeys.deck(deckId) })
    },
  })
}

/**
 * The attempt lifecycle mutations carry no invalidation: attempts are never queried, only
 * started, answered, and finished, and the quiz itself does not change when one is taken.
 */
export function useStartAttempt(quizId: number) {
  return useMutation({
    mutationFn: () => api.startAttempt(quizId),
  })
}

export function useSubmitAnswer(attemptId: number) {
  return useMutation({
    mutationFn: (body: AnswerCreate) => api.submitAnswer(attemptId, body),
  })
}

export function useFinishAttempt(attemptId: number) {
  return useMutation({
    mutationFn: () => api.finishAttempt(attemptId),
  })
}

export function useCreateDeck(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: DeckCreate) => api.createDeck(classId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: studyKeys.list(classId) })
      queryClient.invalidateQueries({ queryKey: classKeys.all })
    },
  })
}

export function useCreateQuiz(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: QuizCreate) => api.createQuiz(classId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: studyKeys.list(classId) })
      queryClient.invalidateQueries({ queryKey: classKeys.all })
    },
  })
}

export function useRenameDeck(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ deckId, title }: { deckId: number; title: string }) =>
      api.renameDeck(deckId, title),
    onSuccess: (deck) => {
      queryClient.invalidateQueries({ queryKey: studyKeys.list(classId) })
      queryClient.invalidateQueries({ queryKey: studyKeys.deck(deck.id) })
    },
  })
}

export function useRenameQuiz(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ quizId, title }: { quizId: number; title: string }) =>
      api.renameQuiz(quizId, title),
    onSuccess: (quiz) => {
      queryClient.invalidateQueries({ queryKey: studyKeys.list(classId) })
      queryClient.invalidateQueries({ queryKey: studyKeys.quiz(quiz.id) })
    },
  })
}

export function useDeleteDeck(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (deckId: number) => api.deleteDeck(deckId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: studyKeys.list(classId) })
    },
  })
}

export function useDeleteQuiz(classId: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (quizId: number) => api.deleteQuiz(quizId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: studyKeys.list(classId) })
    },
  })
}
