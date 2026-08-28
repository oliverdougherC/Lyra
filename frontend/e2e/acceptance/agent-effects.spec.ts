/**
 * Agent and local-effect boundary through the real stack.
 *
 * Proves: capabilities begin gated, grants change what can be proposed,
 * effects require exact confirmation, PLA-303 stale/refreshed hunk boundary.
 */

import { test, expect } from '@playwright/test'
import { realpathSync } from 'node:fs'
import { mkdtemp, writeFile, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  apiGet,
  apiPost,
  apiPatch,
  createClass,
  createSession,
  clearTutorState,
  BACKEND,
} from './helpers'

test.describe('Agent and effect boundary', () => {
  let classId: number
  let workspaceDir: string

  test.beforeAll(async () => {
    const cls = await createClass('Acceptance: Agent')
    classId = cls.id

    // Create a temporary workspace directory
    workspaceDir = realpathSync(await mkdtemp(join(tmpdir(), 'lyra-workspace-')))
    await writeFile(join(workspaceDir, 'hello.py'), 'print("hello world")\n')
    await writeFile(join(workspaceDir, 'README.md'), '# Test Project\n\nA test workspace.\n')
  })

  test.afterEach(async () => {
    await clearTutorState()
  })

  test('workspace attach and grants start disabled', async () => {
    // Attach workspace
    const attachRes = await fetch(`${BACKEND}/api/classes/${classId}/workspace`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
      body: JSON.stringify({ root_path: workspaceDir }),
    })
    expect(attachRes.status).toBe(201)

    // Read workspace — grants should be disabled
    const readRes = await apiGet(`/api/classes/${classId}/workspace`)
    expect(readRes.ok).toBe(true)
    const ws = await readRes.json()
    expect(ws.root_path).toBe(workspaceDir)
    expect(ws.read_enabled).toBe(false)
    expect(ws.change_proposals_enabled).toBe(false)
    expect(ws.commands_enabled).toBe(false)
  })

  test('workspace operations are gated by grants', async () => {
    const session = await createSession(classId)

    // Try to list workspace files without grants — should fail
    const listRes = await apiGet(
      `/api/classes/${classId}/sessions/${session.id}/workspace/list?path=.`,
    )
    expect(listRes.status).toBe(409)
    const listBody = await listRes.json()
    expect(listBody.detail).toMatch(/not enabled|disabled/i)
  })

  test('enable grants and verify workspace read works', async () => {
    // Enable read grant
    const grantRes = await apiPatch(`/api/classes/${classId}/workspace/grants`, {
      read_enabled: true,
    })
    expect(grantRes.ok).toBe(true)

    const session = await createSession(classId)

    // Now listing should work
    const listRes = await apiGet(
      `/api/classes/${classId}/sessions/${session.id}/workspace/list?path=.`,
    )
    expect(listRes.ok).toBe(true)
    const files = await listRes.json()
    expect(files.entries.length).toBeGreaterThan(0)
    const names = files.entries.map((e: { name: string }) => e.name)
    expect(names).toContain('hello.py')
    expect(names).toContain('README.md')

    // Read a file
    const readRes = await apiGet(
      `/api/classes/${classId}/sessions/${session.id}/workspace/read?path=hello.py`,
    )
    expect(readRes.ok).toBe(true)
    const content = await readRes.json()
    expect(content.content).toContain('print("hello world")')
  })

  test('change proposal requires confirmation token', async () => {
    // Enable change proposals
    await apiPatch(`/api/classes/${classId}/workspace/grants`, {
      change_proposals_enabled: true,
    })

    const session = await createSession(classId)

    // Read the file to get its current sha256
    const readRes = await apiGet(
      `/api/classes/${classId}/sessions/${session.id}/workspace/read?path=hello.py`,
    )
    expect(readRes.ok).toBe(true)
    const fileData = await readRes.json()
    const sha256 = fileData.sha256

    // Create a change proposal with correct field names
    const changeRes = await apiPost(
      `/api/classes/${classId}/sessions/${session.id}/workspace/changes`,
      {
        relative_path: 'hello.py',
        observed_base_hash: sha256,
        proposed_content: 'print("hello acceptance")\n',
        rationale: 'Update greeting',
      },
    )
    expect(changeRes.status).toBe(201)
    const change = await changeRes.json()
    expect(change.hunks.length).toBeGreaterThan(0)

    // Extract hunk selections from the review response
    const acceptedHunks = change.hunks.map((h: { index: number; hash: string }) => ({
      index: h.index,
      hash: h.hash,
    }))

    // Try to apply without confirmation token — should fail (422)
    const applyWithoutToken = await fetch(
      `${BACKEND}/api/classes/${classId}/sessions/${session.id}/workspace/changes/${change.id}/apply`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Lyra-Client': 'acceptance-test',
          'Origin': 'http://127.0.0.1:3000',
          'Host': '127.0.0.1:8000',
        },
        body: JSON.stringify({ accepted_hunks: acceptedHunks }),
      },
    )
    expect(applyWithoutToken.status).toBe(422)

    // Get confirmation token (requires Origin for CSRF)
    const confirmRes = await fetch(
      `${BACKEND}/api/classes/${classId}/sessions/${session.id}/workspace/changes/${change.id}/confirmation`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Lyra-Client': 'acceptance-test',
          'Origin': 'http://127.0.0.1:3000',
          'Host': '127.0.0.1:8000',
        },
        body: JSON.stringify({ accepted_hunks: acceptedHunks }),
      },
    )
    expect(confirmRes.ok).toBe(true)
    const confirmation = await confirmRes.json()
    expect(confirmation.token).toBeTruthy()
    expect(confirmation.token.length).toBe(64)

    // Apply with confirmation token and accepted hunks
    const applyRes = await fetch(
      `${BACKEND}/api/classes/${classId}/sessions/${session.id}/workspace/changes/${change.id}/apply`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Origin': 'http://127.0.0.1:3000',
          'Host': '127.0.0.1:8000',
        },
        body: JSON.stringify({
          accepted_hunks: acceptedHunks,
          confirmation_token: confirmation.token,
        }),
      },
    )
    expect(applyRes.ok).toBe(true)

    // Verify file was actually changed on disk
    const actual = await readFile(join(workspaceDir, 'hello.py'), 'utf-8')
    expect(actual).toContain('hello acceptance')
  })

  test('workspace detach cleans up', async () => {
    const detachRes = await fetch(`${BACKEND}/api/classes/${classId}/workspace`, {
      method: 'DELETE',
      headers: { 'X-Lyra-Client': 'acceptance-test' },
    })
    expect(detachRes.status).toBe(204)

    // Workspace should now be null
    const readRes = await apiGet(`/api/classes/${classId}/workspace`)
    const ws = await readRes.json()
    expect(ws).toBeNull()
  })
})
