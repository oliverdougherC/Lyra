/** Real UI source selection and quiz-format contracts (PLA-469, PLA-470). */
import { test, expect, type Page } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  apiGet,
  clearTutorState,
  createClass,
  enqueueTutorResponse,
  getTutorRequests,
  readAcceptanceState,
  uploadDocument,
  waitForDocumentReady,
  waitForStudyReady,
} from './helpers'

const TOPIC = 'Thermodynamics Fundamentals'
const EXCLUDED = 'EXCLUDED_ANSWER_KEY_B'

function questions(type: 'mcq' | 'true_false') {
  return JSON.stringify({
    questions: Array.from({ length: 3 }, (_, index) => ({
      type,
      question: `Which thermodynamic principle applies to closed system ${index + 1}?`,
      options:
        type === 'mcq' ? ['Heat', 'Work', 'Energy conservation', 'Temperature'] : ['True', 'False'],
      correct_index: type === 'mcq' ? 2 : 0,
      explanation: 'A closed system conserves energy through heat and work transfer.',
      topic: TOPIC,
      difficulty: 'intermediate',
    })),
  })
}

// Study read routes omit provenance. Read its durable rows without modifying the database.
async function persisted(artifactId: number) {
  const state = await readAcceptanceState()
  if (!state) throw new Error('Acceptance stack state is unavailable')
  const { dataDir } = state
  return JSON.parse(
    execFileSync(
      'python3',
      [
        '-c',
        `
import json, sqlite3, sys
conn = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)
conn.row_factory = sqlite3.Row
artifact_id = int(sys.argv[2])
print(json.dumps({
  'sources': [r[0] for r in conn.execute('select document_id from artifact_sources where artifact_id = ? order by ordinal', (artifact_id,))],
  'parts': [dict(r) for r in conn.execute('select id from artifact_parts where artifact_id = ?', (artifact_id,))],
  'provenance': [dict(r) for r in conn.execute('select p.part_id, p.document_id, p.chunk_id from artifact_provenance p join artifact_parts a on a.id = p.part_id where a.artifact_id = ?', (artifact_id,))]
}))
`,
        join(dataDir, 'lyra.db'),
        String(artifactId),
      ],
      { encoding: 'utf8' },
    ),
  ) as {
    sources: number[]
    parts: Array<{ id: number }>
    provenance: Array<{ part_id: number; document_id: number; chunk_id: number | null }>
  }
}

test.describe('Selected study material and requested formats', () => {
  let classId: number
  let selectedIds: number[]

  test.beforeEach(async () => {
    const cls = await createClass('Acceptance: Study source contract')
    classId = cls.id
    selectedIds = []
    const directory = await mkdtemp(join(tmpdir(), 'lyra-study-sources-'))
    try {
      const fixtures = [
        [
          'lecture-a.txt',
          `${TOPIC}\nSELECTED_LECTURE_A: A closed system conserves energy. Heat added minus work done equals its internal energy change.`,
        ],
        [
          'lecture-c.txt',
          `${TOPIC}\nSELECTED_LECTURE_C: Entropy in an isolated system increases. Heat flows from hotter to colder bodies.`,
        ],
        [
          'answer-key-b.txt',
          `${EXCLUDED}\n${`${TOPIC}. Thermodynamics fundamentals laws of thermodynamics energy entropy heat work answer key. `.repeat(30)}`,
        ],
      ]
      for (const [filename, content] of fixtures) {
        const path = join(directory, filename)
        await writeFile(path, content)
        const response = await uploadDocument(classId, path, filename)
        expect(response.ok).toBeTruthy()
        const document = await response.json()
        await waitForDocumentReady(document.id)
        if (filename !== 'answer-key-b.txt') selectedIds.push(document.id)
      }
    } finally {
      await rm(directory, { recursive: true, force: true })
    }
    await clearTutorState()
  })

  test.afterEach(clearTutorState)

  async function createFromPicker(page: Page, kind: 'deck' | 'quiz') {
    await page.goto(`/classes/${classId}?tab=practice`)
    await page.getByRole('button', { name: `New ${kind}`, exact: true }).click()
    const dialog = page.getByRole('dialog')
    await dialog.locator('label').filter({ hasText: 'answer-key-b.txt' }).click()
    await expect(dialog.getByRole('checkbox', { name: /answer-key-b.txt/ })).not.toBeChecked()
    await expect(dialog.getByRole('checkbox', { name: /lecture-a.txt/ })).toBeChecked()
    await expect(dialog.getByRole('checkbox', { name: /lecture-c.txt/ })).toBeChecked()
    await dialog.getByRole('button', { name: /Options/ }).click()
    if (kind === 'deck') {
      await dialog.getByLabel('Cards per topic').fill('2')
    } else {
      await dialog.getByLabel('Questions', { exact: true }).fill('3')
      await dialog.getByRole('checkbox', { name: 'True or false', exact: true }).uncheck()
      await dialog.getByRole('checkbox', { name: 'Fill in the blank', exact: true }).uncheck()
      await expect(
        dialog.getByRole('checkbox', { name: 'Multiple choice', exact: true }),
      ).toBeChecked()
    }
    const pending = page.waitForResponse(
      (response) =>
        response
          .url()
          .endsWith(`/api/classes/${classId}/${kind === 'deck' ? 'decks' : 'quizzes'}`) &&
        response.request().method() === 'POST',
    )
    await dialog.getByRole('button', { name: `Create ${kind}`, exact: true }).click()
    const response = await pending
    expect(response.status()).toBe(202)
    const sent = response.request().postDataJSON()
    expect([...sent.document_ids].sort()).toEqual([...selectedIds].sort())
    if (kind === 'quiz') expect(sent.types).toEqual(['mcq'])
    const artifact = await response.json()
    await expect(page).toHaveURL(new RegExp(`/study/${artifact.id}$`))
    return artifact.id as number
  }

  async function assertSelectedPersistence(id: number) {
    const rows = await persisted(id)
    expect([...rows.sources].sort()).toEqual([...selectedIds].sort())
    expect(rows.parts.length).toBeGreaterThan(0)
    for (const part of rows.parts) {
      const provenance = rows.provenance.filter((entry) => entry.part_id === part.id)
      expect(provenance.length).toBeGreaterThan(0)
      for (const entry of provenance) expect(selectedIds).toContain(entry.document_id)
    }
    return rows
  }

  test('PLA-469: selected lectures bound initial and retry flashcard evidence and provenance', async ({
    page,
  }) => {
    await enqueueTutorResponse(JSON.stringify({ topics: [TOPIC] }))
    await enqueueTutorResponse(JSON.stringify({ cards: [] }))
    await enqueueTutorResponse(
      JSON.stringify({
        cards: [
          {
            front: 'What happens to energy in a closed system?',
            back: 'It is conserved.',
            topic: TOPIC,
          },
          {
            front: 'How does entropy change in an isolated system?',
            back: 'It increases.',
            topic: TOPIC,
          },
        ],
      }),
    )
    const id = await createFromPicker(page, 'deck')
    await waitForStudyReady('decks', id)
    await expect(page.getByText(/Card 1 of 2/)).toBeVisible()
    const requests = (await getTutorRequests()).filter((request) =>
      request.url.endsWith('/chat/completions'),
    )
    expect(requests).toHaveLength(3)
    for (const request of requests) {
      const prompt = JSON.stringify(request.body)
      expect(prompt).not.toContain(EXCLUDED)
      expect(prompt).not.toContain('answer-key-b.txt')
      expect(prompt).toContain('SELECTED_LECTURE_')
    }
    const rows = await assertSelectedPersistence(id)
    expect(rows.parts).toHaveLength(2)
    for (const entry of rows.provenance) expect(entry.chunk_id).not.toBeNull()
  })

  test('PLA-470: MCQ-only UI request rejects true/false then persists requested questions', async ({
    page,
  }) => {
    await enqueueTutorResponse(questions('true_false'))
    await enqueueTutorResponse(questions('mcq'))
    const id = await createFromPicker(page, 'quiz')
    await waitForStudyReady('quizzes', id)
    const quiz = await (await apiGet(`/api/quizzes/${id}`)).json()
    expect(quiz.questions).toHaveLength(3)
    for (const { question } of quiz.questions) {
      expect(question.type).toBe('mcq')
      expect(question.correct_index).toBe(2)
      expect(question.options[question.correct_index]).toBe('Energy conservation')
    }
    await assertSelectedPersistence(id)
    const requests = (await getTutorRequests()).filter((request) =>
      request.url.endsWith('/chat/completions'),
    )
    expect(requests).toHaveLength(2)
    for (const request of requests) expect(JSON.stringify(request.body)).not.toContain(EXCLUDED)
    await page.reload()
    await expect(page.getByText(/Question 1 of 3/)).toBeVisible()
    await expect(page.getByRole('button', { name: /Energy conservation/ })).toBeVisible()
  })

  test('PLA-470: repeated out-of-contract formats fail after one retry without publishing questions', async ({
    page,
  }) => {
    await enqueueTutorResponse(questions('true_false'))
    await enqueueTutorResponse(questions('true_false'))
    const id = await createFromPicker(page, 'quiz')
    await expect
      .poll(async () => (await (await apiGet(`/api/quizzes/${id}/status`)).json()).state)
      .toBe('failed')
    await expect(page.getByText('Lyra could not finish writing this')).toBeVisible()
    expect((await apiGet(`/api/quizzes/${id}`)).status).toBe(409)
    const rows = await persisted(id)
    expect([...rows.sources].sort()).toEqual([...selectedIds].sort())
    expect(rows.parts).toEqual([])
    expect(rows.provenance).toEqual([])
    expect(
      (await getTutorRequests()).filter((request) => request.url.endsWith('/chat/completions')),
    ).toHaveLength(2)
  })
})
