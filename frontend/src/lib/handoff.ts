/**
 * Where work moves between Lyra's surfaces.
 *
 * A handoff is a link from the thing the student is looking at to the thing they want to
 * do next: a quiz miss into a conversation about it, a document into a question scoped to
 * it, a class landing into an answer. Building the URLs in one place keeps the contract
 * between the sender and the chat page from drifting apart quietly.
 */

/** Query parameters the chat page consumes once and then strips from the URL. */
export const CHAT_HANDOFF_PARAMS = ['ask', 'send', 'document'] as const

type ChatHandoff = {
  /** Text placed in the composer on arrival. */
  ask?: string
  /**
   * Send `ask` immediately rather than leaving it in the composer. Reserved for the
   * student's own words: a question they typed is a question they asked, while a
   * generated prompt waits for them to look it over first.
   */
  send?: boolean
  /** Scope the conversation's next question to one document. */
  documentId?: number
}

/**
 * A link into a fresh conversation on a class, optionally carrying a question or a
 * document scope. The parameters are consumed by the chat page on arrival and stripped
 * from the URL, so Back and reload never re-ask the question.
 */
export function chatHandoffUrl(classId: number, handoff: ChatHandoff = {}): string {
  const params = new URLSearchParams({ session: 'new' })
  if (handoff.ask) params.set('ask', handoff.ask)
  if (handoff.send) params.set('send', '1')
  if (handoff.documentId !== undefined) params.set('document', String(handoff.documentId))
  return `/classes/${classId}/chat?${params.toString()}`
}

/** "Aug 14", for names that only need to say when the thing was made. */
function shortDate(): string {
  return new Date().toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

/**
 * The name a study artifact gets when the student did not stop to give it one. Dated so
 * two quick sessions in one week stay tellable apart, and renameable afterwards like
 * anything else.
 */
export function quickStudyTitle(kind: 'quiz' | 'deck'): string {
  return kind === 'quiz' ? `Practice · ${shortDate()}` : `Flashcards · ${shortDate()}`
}

/** The name a draft gets when writing starts before naming does. */
export function untitledDraftTitle(): string {
  return `Untitled · ${shortDate()}`
}

/** A study artifact named after the one document it was built from. */
export function documentStudyTitle(filename: string): string {
  return `${filename.replace(/\.[^.]+$/, '')} · practice`
}

/** The question a quiz miss turns into, ready for the tutor. */
export function quizMissQuestion(options: {
  topic: string
  question: string
  chosen: string | null
  correct: string
}): string {
  const opening = `On a practice question about ${options.topic}, I got this wrong: "${options.question}"`
  const answers =
    options.chosen && options.chosen.trim().length > 0
      ? `I answered "${options.chosen}" but the correct answer is "${options.correct}".`
      : `The correct answer is "${options.correct}".`
  return `${opening} ${answers} Can you explain why?`
}

/** The ask a weak quiz topic turns into, from the results screen. */
export function weakTopicQuestion(topic: string): string {
  return `I keep missing questions about ${topic}. Can you walk me through the concept?`
}
