/** Real upload worker -> class chat -> recorded provider request. All pages are synthetic. */
import { execFileSync } from 'node:child_process'
import { mkdtemp, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { expect, test } from '@playwright/test'
import {
  BACKEND,
  apiGet,
  apiPost,
  clearTutorState,
  createClass,
  createSession,
  enqueueTutorResponse,
  getTutorRequests,
  readAcceptanceState,
  waitForDocumentReady,
} from './helpers'

test.describe('Uploaded worksheet evidence reaches the tutor', () => {
  test.describe.configure({ timeout: 120_000 })

  test('mixed native instructions and scanned problems carry coverage and the actual page image', async ({
    page,
  }) => {
    const state = await readAcceptanceState()
    if (!state) throw new Error('An isolated acceptance profile is required')
    const cls = await createClass('Acceptance: worksheet evidence')
    const directory = await mkdtemp(join(tmpdir(), 'lyra-worksheet-'))
    const fixture = join(directory, 'mixed-worksheet.pdf')
    execFileSync(
      'uv',
      [
        'run',
        'python',
        '-c',
        `import pymupdf, sys
pdf = pymupdf.open()
pdf.new_page().insert_text((72, 72), 'Instructions: solve the problems on page 2.')
image_source = pymupdf.open()
scan = image_source.new_page()
scan.insert_text((72, 90), 'Problem 9: find the current through a 12 ohm resistor.')
scan.draw_rect(pymupdf.Rect(72, 120, 290, 190), color=(0, 0, 0))
image = scan.get_pixmap(dpi=144).tobytes('png')
page = pdf.new_page()
page.insert_image(page.rect, stream=image)
pdf.save(sys.argv[1])`,
        fixture,
      ],
      { cwd: resolve(__dirname, '../../..') },
    )

    const form = new FormData()
    form.append('file', new Blob([await readFile(fixture)]), 'mixed-worksheet.pdf')
    const uploaded = await fetch(`${BACKEND}/api/classes/${cls.id}/documents`, {
      method: 'POST',
      headers: { 'X-Lyra-Client': 'acceptance-test', 'X-Idempotency-Key': crypto.randomUUID() },
      body: form,
    })
    expect(uploaded.status).toBe(202)
    const document = await uploaded.json()
    await waitForDocumentReady(document.id, 60_000)
    const detail = await (await apiGet(`/api/documents/${document.id}`)).json()
    expect(detail.coverage_complete).toBe(false)
    expect(detail.page_coverage).toEqual(
      expect.arrayContaining([expect.objectContaining({ page_number: 2, state: 'not_attempted' })]),
    )

    // The settings row belongs to this disposable stack. This simulates an already
    // measured vision-capable endpoint without altering the student's normal settings.
    execFileSync(
      'uv',
      [
        'run',
        'python',
        '-c',
        `import sqlite3, sys
c=sqlite3.connect(sys.argv[1]); c.execute('update settings set vision_supported=1 where id=1'); c.commit(); c.close()`,
        join(state.dataDir, 'lyra.db'),
      ],
      { cwd: resolve(__dirname, '../../..') },
    )
    await clearTutorState()
    await enqueueTutorResponse({ content: 'The current depends on the circuit drawing.' })
    const session = await createSession(cls.id)
    const answer = await apiPost(`/api/classes/${cls.id}/sessions/${session.id}/agent-chat`, {
      content: 'Solve the worksheet problems on page 2',
      document_id: document.id,
      mode: 'show',
    })
    expect(answer.ok).toBe(true)
    const requests = await getTutorRequests()
    const chat = requests.find((request) => {
      const body = request.body as { messages?: unknown[] }
      return Array.isArray(body.messages) && JSON.stringify(body.messages).includes('page 2')
    })
    expect(chat).toBeDefined()
    const messages = (chat!.body as { messages: Array<{ content: unknown }> }).messages
    expect(JSON.stringify(messages[0].content)).toContain('coverage is incomplete')
    const userParts = messages.at(-1)?.content as Array<{
      type: string
      image_url?: { url: string }
    }>
    expect(
      userParts.some(
        (part) =>
          part.type === 'image_url' && part.image_url?.url.startsWith('data:image/png;base64,'),
      ),
    ).toBe(true)

    // The browser reads the same real document list and its partial coverage state.
    await page.goto(`/classes/${cls.id}`)
    await page.getByRole('tab', { name: /^Files/ }).click()
    await expect(page.getByText('mixed-worksheet.pdf')).toBeVisible()
    await expect(page.getByText(/page 2|needs recognition|partially readable/i)).toBeVisible()
    await clearTutorState()
  })

  test('a later-page problem is present in both direct grounding and a document tool continuation', async () => {
    const cls = await createClass('Acceptance: later problem')
    const directory = await mkdtemp(join(tmpdir(), 'lyra-later-problem-'))
    const fixture = join(directory, 'later.pdf')
    execFileSync(
      'uv',
      [
        'run',
        'python',
        '-c',
        `import pymupdf, sys
pdf = pymupdf.open()
for number in range(1, 11):
    page = pdf.new_page()
    page.insert_text((72, 72), 'Worksheet instructions' if number == 1 else
                     'Problem 9(b): use 37 volts across a 12 ohm resistor.' if number == 10 else
                     f'Practice page {number}: explain the example.')
pdf.save(sys.argv[1])`,
        fixture,
      ],
      { cwd: resolve(__dirname, '../../..') },
    )
    const form = new FormData()
    form.append('file', new Blob([await readFile(fixture)]), 'later.pdf')
    const upload = await fetch(`${BACKEND}/api/classes/${cls.id}/documents`, {
      method: 'POST',
      headers: { 'X-Lyra-Client': 'acceptance-test', 'X-Idempotency-Key': crypto.randomUUID() },
      body: form,
    })
    expect(upload.status).toBe(202)
    const document = await upload.json()
    await waitForDocumentReady(document.id, 60_000)

    await clearTutorState()
    await enqueueTutorResponse({
      raw: {
        id: 'tool-round',
        object: 'chat.completion',
        choices: [
          {
            index: 0,
            message: {
              role: 'assistant',
              content: null,
              tool_calls: [
                {
                  id: 'read-problem',
                  type: 'function',
                  function: {
                    name: 'read_document_page',
                    arguments: JSON.stringify({ document_id: document.id, page_number: 10 }),
                  },
                },
              ],
            },
            finish_reason: 'tool_calls',
          },
        ],
      },
    })
    await enqueueTutorResponse({ content: 'The cited page gives 37 volts and 12 ohms.' })
    const session = await createSession(cls.id)
    const answer = await apiPost(`/api/classes/${cls.id}/sessions/${session.id}/agent-chat`, {
      content: 'What about problem 9 part b on page 10?',
      document_id: document.id,
      mode: 'show',
    })
    expect(answer.ok).toBe(true)
    const calls = (await getTutorRequests()).filter(
      (request) => request.url === '/v1/chat/completions',
    )
    expect(calls).toHaveLength(2)
    const first = JSON.stringify((calls[0].body as { messages: unknown[] }).messages)
    const second = JSON.stringify((calls[1].body as { messages: unknown[] }).messages)
    expect(first).toContain('37 volts across a 12 ohm resistor')
    expect(first).toContain('later.pdf')
    expect(second).toContain('37 volts across a 12 ohm resistor')
    expect(second).toContain('page_number')
    await clearTutorState()
  })

  test('short notes remain readable and keyed upload replay does not broaden selected scope', async () => {
    const cls = await createClass('Acceptance: short notes')
    const upload = async (name: string, text: string, key: string) => {
      const form = new FormData()
      form.append('file', new Blob([text]), name)
      return fetch(`${BACKEND}/api/classes/${cls.id}/documents`, {
        method: 'POST',
        headers: { 'X-Lyra-Client': 'acceptance-test', 'X-Idempotency-Key': key },
        body: form,
      })
    }
    const key = crypto.randomUUID()
    const first = await upload('note-a.txt', 'x = 4', key)
    expect(first.status).toBe(202)
    const selected = await first.json()
    await waitForDocumentReady(selected.id, 60_000)
    const second = await upload('note-b.txt', 'x = 9', crypto.randomUUID())
    expect(second.status).toBe(202)
    const other = await second.json()
    await waitForDocumentReady(other.id, 60_000)
    const replay = await upload('note-a.txt', 'x = 4', key)
    expect(replay.status).toBe(202)
    expect((await replay.json()).id).toBe(selected.id)
    const reconciled = await apiGet(`/api/classes/${cls.id}/documents/uploads/${key}`)
    expect(reconciled.ok).toBe(true)
    expect((await reconciled.json()).id).toBe(selected.id)
    const listed = await (await apiGet(`/api/classes/${cls.id}/documents`)).json()
    expect(listed).toHaveLength(2)
    expect(listed.every((item: { coverage_complete: boolean }) => item.coverage_complete)).toBe(
      true,
    )

    await clearTutorState()
    await enqueueTutorResponse({ content: 'x is 4 in the selected note.' })
    const session = await createSession(cls.id)
    const answer = await apiPost(`/api/classes/${cls.id}/sessions/${session.id}/agent-chat`, {
      content: 'What does x equal?',
      document_id: selected.id,
      mode: 'show',
    })
    expect(answer.ok).toBe(true)
    const requests = await getTutorRequests()
    const chat = requests.find((request) => request.url === '/v1/chat/completions')
    expect(chat).toBeDefined()
    const prompt = JSON.stringify((chat!.body as { messages: unknown[] }).messages)
    expect(prompt).toContain('x = 4')
    expect(prompt).not.toContain('x = 9')
    await clearTutorState()
  })

  test('a pure scan reaches the provider as visual evidence before text recognition', async () => {
    const state = await readAcceptanceState()
    if (!state) throw new Error('An isolated acceptance profile is required')
    const cls = await createClass('Acceptance: pure scan')
    const directory = await mkdtemp(join(tmpdir(), 'lyra-pure-scan-'))
    const fixture = join(directory, 'scan.pdf')
    execFileSync(
      'uv',
      [
        'run',
        'python',
        '-c',
        `import pymupdf, sys
source = pymupdf.open()
source.new_page().insert_text((72, 90), 'Problem 4: find 13 plus 29 from the diagram.')
image = source[0].get_pixmap(dpi=144).tobytes('png')
pdf = pymupdf.open()
page = pdf.new_page()
page.insert_image(page.rect, stream=image)
pdf.save(sys.argv[1])`,
        fixture,
      ],
      { cwd: resolve(__dirname, '../../..') },
    )
    const form = new FormData()
    form.append('file', new Blob([await readFile(fixture)]), 'scan.pdf')
    const uploaded = await fetch(`${BACKEND}/api/classes/${cls.id}/documents`, {
      method: 'POST',
      headers: { 'X-Lyra-Client': 'acceptance-test', 'X-Idempotency-Key': crypto.randomUUID() },
      body: form,
    })
    expect(uploaded.status).toBe(202)
    const document = await uploaded.json()
    let detail: { state?: string; page_coverage?: Array<{ state: string }> } = {}
    for (let i = 0; i < 100; i += 1) {
      detail = await (await apiGet(`/api/documents/${document.id}`)).json()
      if (detail.state === 'unsupported') break
      await new Promise((resolve) => setTimeout(resolve, 100))
    }
    expect(detail.state).toBe('unsupported')
    expect(detail.page_coverage).toEqual(
      expect.arrayContaining([expect.objectContaining({ state: 'not_attempted' })]),
    )
    execFileSync(
      'uv',
      [
        'run',
        'python',
        '-c',
        `import sqlite3, sys
c=sqlite3.connect(sys.argv[1]); c.execute('update settings set vision_supported=1 where id=1'); c.commit(); c.close()`,
        join(state.dataDir, 'lyra.db'),
      ],
      { cwd: resolve(__dirname, '../../..') },
    )
    await clearTutorState()
    await enqueueTutorResponse({ content: 'The scan contains the problem on page one.' })
    const session = await createSession(cls.id)
    const answer = await apiPost(`/api/classes/${cls.id}/sessions/${session.id}/agent-chat`, {
      content: 'Solve the scanned worksheet',
      document_id: document.id,
      mode: 'show',
    })
    expect(answer.ok).toBe(true)
    const chat = (await getTutorRequests()).find(
      (request) => request.url === '/v1/chat/completions',
    )
    expect(chat).toBeDefined()
    const messages = (chat!.body as { messages: Array<{ content: unknown }> }).messages
    const parts = messages.at(-1)?.content as Array<{ type: string; image_url?: { url: string } }>
    expect(
      parts.some(
        (part) =>
          part.type === 'image_url' && part.image_url?.url.startsWith('data:image/png;base64,'),
      ),
    ).toBe(true)
    expect(JSON.stringify(messages[0].content)).toContain('coverage is incomplete')
    await clearTutorState()
  })

  test('native matrix text also carries its page layout to the provider', async () => {
    const state = await readAcceptanceState()
    if (!state) throw new Error('An isolated acceptance profile is required')
    const cls = await createClass('Acceptance: matrix layout')
    const directory = await mkdtemp(join(tmpdir(), 'lyra-matrix-'))
    const fixture = join(directory, 'matrix.pdf')
    execFileSync(
      'uv',
      [
        'run',
        'python',
        '-c',
        `import pymupdf, sys
pdf = pymupdf.open()
page = pdf.new_page()
page.insert_text((72, 72), 'Matrix A has these four entries; find its determinant.')
page.insert_text((100, 110), '2       7')
page.insert_text((100, 134), '5       11')
pdf.save(sys.argv[1])`,
        fixture,
      ],
      { cwd: resolve(__dirname, '../../..') },
    )
    const form = new FormData()
    form.append('file', new Blob([await readFile(fixture)]), 'matrix.pdf')
    const uploaded = await fetch(`${BACKEND}/api/classes/${cls.id}/documents`, {
      method: 'POST',
      headers: { 'X-Lyra-Client': 'acceptance-test', 'X-Idempotency-Key': crypto.randomUUID() },
      body: form,
    })
    expect(uploaded.status).toBe(202)
    const document = await uploaded.json()
    await waitForDocumentReady(document.id, 60_000)
    execFileSync(
      'uv',
      [
        'run',
        'python',
        '-c',
        `import sqlite3, sys
c=sqlite3.connect(sys.argv[1]); c.execute('update settings set vision_supported=1 where id=1'); c.commit(); c.close()`,
        join(state.dataDir, 'lyra.db'),
      ],
      { cwd: resolve(__dirname, '../../..') },
    )
    await clearTutorState()
    await enqueueTutorResponse({ content: 'The determinant uses the arranged entries.' })
    const session = await createSession(cls.id)
    const answer = await apiPost(`/api/classes/${cls.id}/sessions/${session.id}/agent-chat`, {
      content: 'Explain this matrix on page 1',
      document_id: document.id,
      mode: 'show',
    })
    expect(answer.ok).toBe(true)
    const chat = (await getTutorRequests()).find(
      (request) => request.url === '/v1/chat/completions',
    )
    expect(chat).toBeDefined()
    const messages = (chat!.body as { messages: Array<{ content: unknown }> }).messages
    expect(JSON.stringify(messages[0].content)).toContain('Matrix A')
    const parts = messages.at(-1)?.content as Array<{ type: string; image_url?: { url: string } }>
    expect(
      parts.some(
        (part) =>
          part.type === 'image_url' && part.image_url?.url.startsWith('data:image/png;base64,'),
      ),
    ).toBe(true)
    await clearTutorState()
  })
})
