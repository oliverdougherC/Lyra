/**
 * Agent and local-effect boundary through the real stack.
 *
 * Proves: capabilities begin gated, grants change what can be proposed,
 * effects require exact confirmation, PLA-303 stale hunk race (display hunk,
 * mutate workspace on disk, attempt approval with stale hash -- proves
 * rejection).
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

    workspaceDir = realpathSync(await mkdtemp(join(tmpdir(), 'lyra-workspace-')))
    await writeFile(join(workspaceDir, 'hello.py'), 'print("hello world")\n')
    await writeFile(join(workspaceDir, 'README.md'), '# Test Project\n\nA test workspace.\n')
  })

  test.afterEach(async () => {
    await clearTutorState()
  })

  test('workspace attach and grants start disabled', async () => {
    const attachRes = await fetch(`${BACKEND}/api/classes/${classId}/workspace`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'X-Lyra-Client': 'acceptance-test',
      },
      body: JSON.stringify({ root_path: workspaceDir }),
    })
    expect(attachRes.status).toBe(201)

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

    const listRes = await apiGet(
      `/api/classes/${classId}/sessions/${session.id}/workspace/list?path=.`,
    )
    expect(listRes.status).toBe(409)
    const listBody = await listRes.json()
    expect(listBody.detail).toMatch(/not enabled|disabled/i)
  })

  test('enable grants and verify workspace read works', async () => {
    const grantRes = await apiPatch(`/api/classes/${classId}/workspace/grants`, {
      read_enabled: true,
    })
    expect(grantRes.ok).toBe(true)

    const session = await createSession(classId)

    const listRes = await apiGet(
      `/api/classes/${classId}/sessions/${session.id}/workspace/list?path=.`,
    )
    expect(listRes.ok).toBe(true)
    const files = await listRes.json()
    expect(files.entries.length).toBeGreaterThan(0)
    const names = files.entries.map((e: { name: string }) => e.name)
    expect(names).toContain('hello.py')
    expect(names).toContain('README.md')

    const readRes = await apiGet(
      `/api/classes/${classId}/sessions/${session.id}/workspace/read?path=hello.py`,
    )
    expect(readRes.ok).toBe(true)
    const content = await readRes.json()
    expect(content.content).toContain('print("hello world")')
  })

  test('change proposal requires confirmation token and applies on disk', async () => {
    await apiPatch(`/api/classes/${classId}/workspace/grants`, {
      change_proposals_enabled: true,
    })

    const session = await createSession(classId)

    const readRes = await apiGet(
      `/api/classes/${classId}/sessions/${session.id}/workspace/read?path=hello.py`,
    )
    expect(readRes.ok).toBe(true)
    const fileData = await readRes.json()
    const sha256 = fileData.sha256

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

    const acceptedHunks = change.hunks.map((h: { index: number; hash: string }) => ({
      index: h.index,
      hash: h.hash,
    }))

    // Without confirmation token -- should fail
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

    // Get confirmation token
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

    // Apply with token
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

    const actual = await readFile(join(workspaceDir, 'hello.py'), 'utf-8')
    expect(actual).toContain('hello acceptance')
  })

  test('PLA-303: stale hunk rejected after workspace file changes on disk', async () => {
    // Reset file to known state
    await writeFile(join(workspaceDir, 'hello.py'), 'print("hello acceptance")\n')

    const session = await createSession(classId)

    // Read current file state
    const readRes = await apiGet(
      `/api/classes/${classId}/sessions/${session.id}/workspace/read?path=hello.py`,
    )
    const fileData = await readRes.json()

    // Propose a change based on the current file state
    const changeRes = await apiPost(
      `/api/classes/${classId}/sessions/${session.id}/workspace/changes`,
      {
        relative_path: 'hello.py',
        observed_base_hash: fileData.sha256,
        proposed_content: 'print("hello stale")\n',
        rationale: 'This will become stale',
      },
    )
    expect(changeRes.status).toBe(201)
    const change = await changeRes.json()
    const staleHunks = change.hunks.map((h: { index: number; hash: string }) => ({
      index: h.index,
      hash: h.hash,
    }))

    // MUTATE the workspace file on disk -- simulating an external edit or
    // another change that happened between display and approval
    await writeFile(join(workspaceDir, 'hello.py'), 'print("hello mutated externally")\n')

    // Try to get confirmation with the stale hunk hashes -- the backend
    // should detect that the file has changed and reject
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
        body: JSON.stringify({ accepted_hunks: staleHunks }),
      },
    )

    // The backend should reject because the file is no longer fresh
    if (confirmRes.ok) {
      // If confirmation succeeds (some implementations check at apply time),
      // the apply step should fail
      const confirmation = await confirmRes.json()
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
            accepted_hunks: staleHunks,
            confirmation_token: confirmation.token,
          }),
        },
      )
      // Must NOT succeed -- the hunk was stale
      expect(applyRes.ok).toBe(false)
      expect(applyRes.status).toBe(409)
    } else {
      // Confirmation itself rejected -- also correct
      expect(confirmRes.status).toBe(409)
    }

    // Verify the file on disk was NOT modified by the stale proposal
    const diskContent = await readFile(join(workspaceDir, 'hello.py'), 'utf-8')
    expect(diskContent).toBe('print("hello mutated externally")\n')
  })

  test('workspace detach cleans up', async () => {
    const detachRes = await fetch(`${BACKEND}/api/classes/${classId}/workspace`, {
      method: 'DELETE',
      headers: { 'X-Lyra-Client': 'acceptance-test' },
    })
    expect(detachRes.status).toBe(204)

    const readRes = await apiGet(`/api/classes/${classId}/workspace`)
    const ws = await readRes.json()
    expect(ws).toBeNull()
  })
})
