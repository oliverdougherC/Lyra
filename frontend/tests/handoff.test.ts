import { describe, expect, it } from 'vitest'

import {
  chatHandoffUrl,
  documentStudyTitle,
  quickStudyTitle,
  quizMissQuestion,
  readChatHandoff,
  stripChatHandoff,
  untitledDraftTitle,
  weakTopicQuestion,
} from '@/lib/handoff'

describe('chatHandoffUrl', () => {
  it('always opens a fresh conversation', () => {
    expect(chatHandoffUrl(3)).toBe('/classes/3/chat?session=new')
  })

  it('carries a question, URL-encoded, and marks it for sending only when asked to', () => {
    const prefill = chatHandoffUrl(3, { ask: 'Why does convolution flip the signal?' })
    expect(prefill).toContain('ask=Why+does+convolution+flip+the+signal%3F')
    expect(prefill).not.toContain('send=1')

    const sent = chatHandoffUrl(3, { ask: 'Why?', send: true })
    expect(sent).toContain('send=1')
  })

  it('scopes to a document by id', () => {
    expect(chatHandoffUrl(3, { documentId: 41 })).toBe('/classes/3/chat?session=new&document=41')
  })
})

describe('quick titles', () => {
  const MOMENT = /\w{3} \d{1,2}, \d{1,2}:\d{2}\s?[AP]M$/

  it('stamps quick creates to the minute so repeated sessions stay tellable apart', () => {
    // Date alone was not enough: three practice runs before one exam became three
    // artifacts with the same name.
    expect(quickStudyTitle('quiz')).toMatch(new RegExp(`^Practice · ${MOMENT.source}`))
    expect(quickStudyTitle('deck')).toMatch(new RegExp(`^Flashcards · ${MOMENT.source}`))
    expect(untitledDraftTitle()).toMatch(new RegExp(`^Untitled · ${MOMENT.source}`))
  })

  it('names a document practice set after the file, extension dropped', () => {
    expect(documentStudyTitle('week_4_notes.pdf')).toBe('week_4_notes · practice')
  })
})

describe('reading and stripping a handoff', () => {
  it('round-trips what chatHandoffUrl wrote', () => {
    const url = chatHandoffUrl(3, { ask: 'Why?', send: true, documentId: 41 })
    const params = new URLSearchParams(url.split('?')[1])
    expect(readChatHandoff(params)).toEqual({ ask: 'Why?', send: true, documentId: 41 })
  })

  it('reads absence as absence', () => {
    expect(readChatHandoff(new URLSearchParams('session=new'))).toEqual({
      ask: null,
      send: false,
      documentId: null,
    })
    expect(readChatHandoff(new URLSearchParams('document=junk'))).toMatchObject({
      documentId: null,
    })
  })

  it('strips every handoff param and keeps the rest of the query', () => {
    const stripped = stripChatHandoff(
      new URLSearchParams('session=12&ask=Why%3F&send=1&document=4'),
    )
    expect(stripped?.toString()).toBe('session=12')
  })

  it('reports when there is nothing to strip, so the page does not rewrite the URL', () => {
    expect(stripChatHandoff(new URLSearchParams('session=12'))).toBeNull()
  })
})

describe('quiz handoff questions', () => {
  it('carries the question, both answers, and the topic', () => {
    const ask = quizMissQuestion({
      topic: 'Determinants',
      question: 'What is det(I)?',
      chosen: 'Zero',
      correct: 'One',
    })
    expect(ask).toContain('Determinants')
    expect(ask).toContain('What is det(I)?')
    expect(ask).toContain('"Zero"')
    expect(ask).toContain('"One"')
  })

  it('drops the chosen-answer clause when nothing was chosen', () => {
    const ask = quizMissQuestion({
      topic: 'Determinants',
      question: 'What is det(I)?',
      chosen: null,
      correct: 'One',
    })
    expect(ask).not.toContain('I answered')
    expect(ask).toContain('"One"')
  })

  it('asks about a weak topic in plain words', () => {
    expect(weakTopicQuestion('Fourier series')).toContain('Fourier series')
  })
})
