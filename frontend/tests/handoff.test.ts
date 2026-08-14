import { describe, expect, it } from 'vitest'

import {
  chatHandoffUrl,
  documentStudyTitle,
  quickStudyTitle,
  quizMissQuestion,
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
  it('dates a quick study session so two in one week stay tellable apart', () => {
    expect(quickStudyTitle('quiz')).toMatch(/^Practice · \w{3} \d{1,2}$/)
    expect(quickStudyTitle('deck')).toMatch(/^Flashcards · \w{3} \d{1,2}$/)
    expect(untitledDraftTitle()).toMatch(/^Untitled · \w{3} \d{1,2}$/)
  })

  it('names a document practice set after the file, extension dropped', () => {
    expect(documentStudyTitle('week_4_notes.pdf')).toBe('week_4_notes · practice')
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
