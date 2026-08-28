/**
 * Launch, classes, and document lifecycle through the real stack.
 *
 * Proves: clean startup, class CRUD, supported/unsupported uploads, ingestion
 * to ready, provenance, move, delete, re-upload — all through the real FastAPI
 * backend, SQLite, and background workers.
 */

import { test, expect } from '@playwright/test'
import { resolve } from 'node:path'
import { apiGet, apiDelete, createClass, uploadDocument, waitForDocumentReady } from './helpers'

const TEST_DATA = resolve(__dirname, 'test-data')

test.describe('Launch and documents', () => {
  let classId: number

  test.beforeAll(async () => {
    const cls = await createClass('Acceptance: Thermodynamics')
    classId = cls.id
  })

  test('health endpoint reports ready', async () => {
    const res = await apiGet('/api/health/ready')
    expect(res.ok).toBe(true)
    const body = await res.json()
    expect(body.status).toBe('ready')
    expect(body.components.database.status).toBe('ready')
  })

  test('home page renders the created class', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('heading', { name: 'Acceptance: Thermodynamics' })).toBeVisible()
  })

  test('upload supported text document and reach ready', async () => {
    const res = await uploadDocument(classId, resolve(TEST_DATA, 'sample.txt'), 'sample.txt')
    expect(res.status).toBe(202)
    const doc = await res.json()
    expect(doc.id).toBeGreaterThan(0)
    expect(doc.state).toBe('pending')

    await waitForDocumentReady(doc.id, 30_000)

    // Verify provenance
    const detail = await apiGet(`/api/documents/${doc.id}`)
    const body = await detail.json()
    expect(body.state).toBe('ready')
    expect(body.filename).toBe('sample.txt')
    expect(body.class_id).toBe(classId)
    expect(body.byte_size).toBeGreaterThan(0)
  })

  test('upload supported markdown document', async () => {
    const res = await uploadDocument(classId, resolve(TEST_DATA, 'supplement.md'), 'supplement.md')
    expect(res.status).toBe(202)
    const doc = await res.json()
    await waitForDocumentReady(doc.id, 30_000)
  })

  test('upload unsupported file type returns 400', async () => {
    const res = await uploadDocument(
      classId,
      resolve(TEST_DATA, 'unsupported.docx'),
      'unsupported.docx',
    )
    expect(res.status).toBe(400)
    const body = await res.json()
    expect(body.detail).toMatch(/unsupported|not supported/i)
  })

  test('document list reflects ingested documents', async () => {
    const res = await apiGet(`/api/classes/${classId}/documents`)
    expect(res.ok).toBe(true)
    const docs = await res.json()
    expect(docs.length).toBeGreaterThanOrEqual(2)
    const names = docs.map((d: { filename: string }) => d.filename)
    expect(names).toContain('sample.txt')
    expect(names).toContain('supplement.md')
  })

  test('move document to another class', async () => {
    // Create a second class
    const cls2 = await createClass('Acceptance: Target Class')

    // Get the supplement document
    const listRes = await apiGet(`/api/classes/${classId}/documents`)
    const docs = await listRes.json()
    const supplement = docs.find((d: { filename: string }) => d.filename === 'supplement.md')
    expect(supplement).toBeDefined()

    // Move it
    const moveRes = await fetch(`http://127.0.0.1:8000/api/documents/${supplement.id}/move`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
      body: JSON.stringify({ class_id: cls2.id }),
    })
    expect(moveRes.status).toBe(202)

    // Wait for re-ingestion in new class
    await waitForDocumentReady(supplement.id, 30_000)

    // Verify it moved
    const detail = await apiGet(`/api/documents/${supplement.id}`)
    const body = await detail.json()
    expect(body.class_id).toBe(cls2.id)

    // Original class no longer lists it
    const origDocs = await apiGet(`/api/classes/${classId}/documents`)
    const origList = await origDocs.json()
    expect(origList.find((d: { id: number }) => d.id === supplement.id)).toBeUndefined()

    // Clean up: delete the target class
    await apiDelete(`/api/classes/${cls2.id}`)
  })

  test('delete document and re-upload', async () => {
    // Get a document to delete
    const listRes = await apiGet(`/api/classes/${classId}/documents`)
    const docs = await listRes.json()
    const sample = docs.find((d: { filename: string }) => d.filename === 'sample.txt')
    expect(sample).toBeDefined()

    // Delete it
    const delRes = await apiDelete(`/api/documents/${sample.id}`)
    expect(delRes.status).toBe(204)

    // Verify it's gone
    const afterDocs = await apiGet(`/api/classes/${classId}/documents`)
    const afterList = await afterDocs.json()
    expect(afterList.find((d: { id: number }) => d.id === sample.id)).toBeUndefined()

    // Re-upload
    const reuploadRes = await uploadDocument(
      classId,
      resolve(TEST_DATA, 'sample.txt'),
      'sample.txt',
    )
    expect(reuploadRes.status).toBe(202)
    const newDoc = await reuploadRes.json()
    await waitForDocumentReady(newDoc.id, 30_000)

    // Verify re-uploaded document is ready
    const newDetail = await apiGet(`/api/documents/${newDoc.id}`)
    const newBody = await newDetail.json()
    expect(newBody.state).toBe('ready')
    expect(newBody.id).not.toBe(sample.id) // new row
  })

  test('documents tab in class hub shows documents', async ({ page }) => {
    await page.goto(`/classes/${classId}`)
    await page.getByRole('tab', { name: /documents/i }).click()
    await expect(page.getByText('sample.txt')).toBeVisible()
  })
})
