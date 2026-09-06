/** Real HTTP + browser download coverage; native save-dialog behavior is a separate check. */
import { execFileSync } from 'node:child_process'
import { readFile, unlink } from 'node:fs/promises'
import { resolve } from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import { apiGet, createClass, readAcceptanceState } from './helpers'

type RecoveryFixture = {
  classId: number
  solutionId: number
  documentId: number
  originalPath: string
  filename: string
}

async function seedRecovery(): Promise<RecoveryFixture> {
  const state = await readAcceptanceState()
  if (!state) throw new Error('Original recovery requires the isolated acceptance data directory')
  const cls = await createClass('Acceptance: original recovery')
  // Seed damaged derived metadata, not intercepted responses: the real renderer cannot
  // find page 2 in this valid one-page original, and no extracted-text file exists.
  // Its expected 404 is deliberately not a 5xx and needs no failure-ledger exemption.
  const seeded = execFileSync(
    'uv',
    [
      'run',
      'python',
      '-c',
      `import json, sqlite3, sys
from pathlib import Path
import pymupdf
root, class_id = Path(sys.argv[1]), int(sys.argv[2])
conn = sqlite3.connect(root / 'lyra.db')
conn.execute('pragma foreign_keys = on')
solution_id = conn.execute(
    "insert into artifacts (class_id, kind, title, state, problems_total, problems_done) values (?, 'solution_set', 'Original recovery context', 'ready', 2, 2)",
    (class_id,),
).lastrowid
for ordinal in range(2):
    filename = f'recovery-{ordinal + 1}.pdf'
    document_id = conn.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state, pages_total) values (?, ?, '', 'application/pdf', 0, 'ready', 3)",
        (class_id, filename),
    ).lastrowid
    original = root / 'uploads' / str(class_id) / f'{document_id}-{filename}'
    original.parent.mkdir(parents=True, exist_ok=True)
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), f'Actual original document {ordinal + 1}')
    pdf.save(original)
    pdf.close()
    conn.execute('update documents set stored_path = ?, byte_size = ? where id = ?',
                 (str(original), original.stat().st_size, document_id))
    conn.execute("insert into artifact_sources (artifact_id, document_id, role, ordinal) values (?, ?, 'problem_set', ?)",
                 (solution_id, document_id, ordinal))
    part_id = conn.execute(
        "insert into artifact_parts (artifact_id, kind, ordinal, label, content, status) values (?, 'problem', ?, ?, ?, 'complete')",
        (solution_id, ordinal, f'Problem {ordinal + 1}', f'Preserve problem {ordinal + 1} context.'),
    ).lastrowid
    conn.execute('insert into artifact_provenance (part_id, document_id, page_number) values (?, ?, 2)',
                 (part_id, document_id))
conn.commit()
conn.close()
print(json.dumps(dict(solutionId=solution_id, documentId=document_id, originalPath=str(original), filename=filename)))`,
      state.dataDir,
      String(cls.id),
    ],
    { cwd: resolve(__dirname, '../../..'), encoding: 'utf-8' },
  )
  return { classId: cls.id, ...JSON.parse(seeded) }
}

async function showDoubleFailure(page: Page, fixture: RecoveryFixture) {
  // Source text can load while document metadata resolves, before the PDF fallback
  // is clicked. Observe that real response before navigation can populate its cache.
  const textResponse = page.waitForResponse((response) =>
    response.url().endsWith(`/api/documents/${fixture.documentId}/text`),
  )
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto(`/classes/${fixture.classId}/solutions/${fixture.solutionId}`)
  await page
    .getByRole('navigation', { name: 'Problems in this set' })
    .getByTitle('Problem 2')
    .click()
  await expect(page.getByRole('combobox', { name: 'Source document', exact: true })).toHaveValue(
    String(fixture.documentId),
  )
  await expect(page.getByText('page 2 of 3', { exact: true })).toBeVisible()
  await expect(page.getByText('That page could not be rendered.', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Read extracted text', exact: true }).click()
  const response = await textResponse
  expect(response.status()).toBe(200)
  expect((await response.json()).text).toBe('')
  await expect(
    page.getByRole('button', { name: 'Save original document', exact: true }),
  ).toBeVisible()
}

async function expectContext(page: Page, fixture: RecoveryFixture) {
  await expect(page).toHaveURL(`/#/classes/${fixture.classId}/solutions/${fixture.solutionId}`)
  await expect(page.getByRole('combobox', { name: 'Source document', exact: true })).toHaveValue(
    String(fixture.documentId),
  )
  await expect(
    page.getByRole('navigation', { name: 'Problems in this set' }).getByTitle('Problem 2'),
  ).toHaveAttribute('aria-current', 'true')
  await page.getByRole('button', { name: 'View page', exact: true }).click()
  await expect(page.getByText('page 2 of 3', { exact: true })).toBeVisible()
  await expect(page.getByText('That page could not be rendered.', { exact: true })).toBeVisible()
}

test('double failure recovers the actual original bytes and keeps source/page/problem context', async ({
  page,
}, testInfo) => {
  const fixture = await seedRecovery()
  await showDoubleFailure(page, fixture)
  await page.screenshot({ path: testInfo.outputPath('source-recovery.png') })
  const originalRequest = page.waitForRequest(
    (request) =>
      new URL(request.url()).pathname === `/api/documents/${fixture.documentId}/original`,
  )
  const downloadStarted = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Save original document', exact: true }).click()
  const request = await originalRequest
  expect(new URL(request.url()).search).toBe('')
  expect((await request.allHeaders()).origin).toBe(new URL(page.url()).origin)
  const download = await downloadStarted
  expect(download.suggestedFilename()).toBe(fixture.filename)
  expect(await download.failure()).toBeNull()
  const downloadedPath = await download.path()
  expect(downloadedPath).not.toBeNull()
  expect(await readFile(downloadedPath!)).toEqual(await readFile(fixture.originalPath))
  await expect(page.getByText('Original document download started.', { exact: true })).toBeVisible()
  await expectContext(page, fixture)
})

test('unavailable original reports the failure honestly and keeps source/page/problem context', async ({
  page,
}) => {
  const fixture = await seedRecovery()
  await showDoubleFailure(page, fixture)
  await unlink(fixture.originalPath)
  const downloads: string[] = []
  page.on('download', (download) => downloads.push(download.suggestedFilename()))
  const originalResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `/api/documents/${fixture.documentId}/original`,
  )
  await page.getByRole('button', { name: 'Save original document', exact: true }).click()
  expect((await originalResponse).status()).toBe(404)
  await expect(
    page.getByText('The original document is missing or inaccessible.', { exact: true }),
  ).toBeVisible()
  expect(downloads).toEqual([])
  await expect(page.getByText('Original document download started.', { exact: true })).toHaveCount(
    0,
  )
  expect(await page.locator('body').innerText()).not.toContain(fixture.originalPath)
  await expectContext(page, fixture)
  const detail = await (await apiGet(`/api/solutions/${fixture.solutionId}`)).json()
  expect(
    detail.sources.some(
      (source: { document_id: number }) => source.document_id === fixture.documentId,
    ),
  ).toBe(true)
})
