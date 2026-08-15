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

/** What one arrival at the chat page carries, read out of its query string. */
export type ConsumedHandoff = {
  ask: string | null
  send: boolean
  documentId: number | null
}

/**
 * Reads a handoff out of the chat page's search params, exactly as the links built by
 * `chatHandoffUrl` wrote it. The page calls this once, on the render it arrives on: a
 * handoff is an argument to the navigation, not a live piece of URL state, which is why
 * the same links must only ever be offered from *other* routes. A link to the chat route
 * from inside the chat route would change the params without remounting the page, and the
 * handoff would be stripped without being applied.
 */
export function readChatHandoff(params: URLSearchParams): ConsumedHandoff {
  const rawDocument = Number(params.get('document'))
  return {
    ask: params.get('ask'),
    send: params.get('send') === '1',
    documentId: Number.isSafeInteger(rawDocument) && rawDocument > 0 ? rawDocument : null,
  }
}

/**
 * The same params with the handoff removed, or null when there was none to strip. What
 * remains (the session, once it exists) stays; replacing the URL with this is what makes
 * refresh and Back re-open the conversation instead of re-asking the question.
 */
export function stripChatHandoff(params: URLSearchParams): URLSearchParams | null {
  if (!CHAT_HANDOFF_PARAMS.some((param) => params.has(param))) return null
  const next = new URLSearchParams(params)
  for (const param of CHAT_HANDOFF_PARAMS) next.delete(param)
  return next
}

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

/**
 * "Aug 14, 3:42 PM", for names that only need to say when the thing was made. Minutes
 * matter: the whole point of a quick create is using it more than once, and three
 * practice runs before Friday's exam must not become three artifacts with one name.
 */
function shortMoment(): string {
  return new Date().toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

/**
 * The name itself when nothing has taken it, otherwise the first of "name · 2",
 * "name · 3" that nothing has. A minute stamp is not unique enough on its own: start,
 * Back, start again fits inside one minute easily, and the second artifact must not
 * wear the first one's name. The suffix is a plain count rather than seconds or an id,
 * because these names exist to be recognized, not to be unique forever - a rename
 * replaces them the moment the work earns a real one.
 */
export function uniqueTitle(base: string, existing: Iterable<string>): string {
  const taken = new Set(existing)
  if (!taken.has(base)) return base
  let copy = 2
  while (taken.has(`${base} · ${copy}`)) copy += 1
  return `${base} · ${copy}`
}

/**
 * The name a study artifact gets when the student did not stop to give it one. Stamped
 * to the minute so repeated sessions stay tellable apart, numbered past any sibling
 * already wearing the same minute, and renameable afterwards like anything else.
 */
export function quickStudyTitle(kind: 'quiz' | 'deck', existing: Iterable<string> = []): string {
  const base = kind === 'quiz' ? `Practice · ${shortMoment()}` : `Flashcards · ${shortMoment()}`
  return uniqueTitle(base, existing)
}

/** The name a draft gets when writing starts before naming does. */
export function untitledDraftTitle(existing: Iterable<string> = []): string {
  return uniqueTitle(`Untitled · ${shortMoment()}`, existing)
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
