/**
 * The final PLA-401 journey through the real stack: ONE ordinary class conversation.
 *
 * The student talks to Lyra in the normal class chat composer only. There is no agent
 * destination, no second composer, no profile chooser, and no grant dashboard: the
 * agent's just-in-time access request, the attached-workspace chip, the file-change
 * review, the command approval, and the result all live contextually in the same
 * conversation - the work surface above the transcript, and the composer's context row.
 *
 * Proves, end to end through the real backend and a scripted tutor endpoint:
 *  1. an ordinary message asking for workspace inspection;
 *  2. the just-in-time access request card with the model's task-specific reason;
 *  3. folder attach through the bounded path entry (the browser build has no native
 *     picker; desktop uses the picker for the same action) and the workspace chip in
 *     the composer's context row;
 *  4. the interrupted turn continuing itself: with the access in hand the surface
 *     re-answers the student's own question, in the same transcript;
 *  5. an ordinary follow-up asking for an edit: the change proposal appears contextually
 *     and nothing is applied until the student accepts;
 *  6. the exact verification command: it appears with argv, cwd, and purpose, and
 *     nothing runs until the student confirms and runs it;
 *  7. the command result in the same conversation;
 *  8. bounded "Not now": a dismissal hides the request for the session without granting
 *     anything.
 */

import { createHash } from 'node:crypto'
import { realpathSync } from 'node:fs'
import { mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, basename } from 'node:path'

import { test, expect } from '@playwright/test'
import {
  apiDelete,
  apiGet,
  createClass,
  createSession,
  clearTutorState,
  enqueueTutorResponse,
  sendChatMessage,
} from './helpers'

const PARSER_BASE = 'def parse(text):\n    raise NotImplementedError\n'

const PARSER_SKELETON = 'def parse(text):\n    return text.split()\n'

const TEST_PARSER = [
  'from parser import parse',
  '',
  'assert parse("a b") == ["a", "b"]',
  'print("tests passed")',
  '',
].join('\n')

const MAIN_PY = 'print("starter project")\n'

function toolCallCompletion(name: string, args: unknown): Record<string, unknown> {
  return {
    id: `chatcmpl-agent-${Date.now()}-${Math.random().toString(36).slice(2)}`,
    object: 'chat.completion',
    choices: [
      {
        index: 0,
        message: {
          role: 'assistant',
          content: null,
          tool_calls: [
            {
              id: `call_${Math.random().toString(36).slice(2)}`,
              type: 'function',
              function: { name, arguments: JSON.stringify(args) },
            },
          ],
        },
        finish_reason: 'tool_calls',
      },
    ],
    usage: { prompt_tokens: 100, completion_tokens: 40, total_tokens: 140 },
  }
}

test.describe('One ordinary class conversation (PLA-401)', () => {
  test.describe.configure({ timeout: 180_000 })

  let classId: number
  let workspaceDir: string

  test.beforeAll(async () => {
    const cls = await createClass('Acceptance: Agent Conversation')
    classId = cls.id

    // The starter project the conversation is about. It is seeded but NOT attached:
    // attaching it is the student's action, mid-conversation.
    workspaceDir = realpathSync(await mkdtemp(join(tmpdir(), 'lyra-journey-')))
    await writeFile(join(workspaceDir, 'main.py'), MAIN_PY)
    await writeFile(join(workspaceDir, 'parser.py'), PARSER_BASE)
    await writeFile(join(workspaceDir, 'test_parser.py'), TEST_PARSER)
  })

  test.afterEach(async () => {
    await clearTutorState()
  })

  test('asks for inspection, attaches mid-conversation, and continues in the same transcript', async ({
    page,
  }) => {
    const session = await createSession(classId)
    await page.goto(`/classes/${classId}/chat?session=${session.id}`)
    await page.waitForLoadState('networkidle')

    // Before any work is live, there is no work surface at all: the conversation is the
    // surface. The composer's context row carries the workspace affordance.
    await expect(page.locator('[aria-label="Agent work"]')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Attach folder' })).toBeVisible()

    // Script the whole turn-1 + continuation sequence, in model-call order:
    //  1. the turn's first round asks for the folder (the snapshot admits no workspace);
    //  2. its second round answers plainly that it is waiting;
    //  3. the continued turn lists the workspace with the newly granted read access;
    //  4. ...and answers the student's own question in the same transcript.
    await enqueueTutorResponse({
      raw: toolCallCompletion('request_workspace_access', {
        scope: 'attach',
        reason: 'To inspect this starter project, Lyra needs to open the folder.',
      }),
    })
    await enqueueTutorResponse({
      content:
        'I need to open the project folder before I can explain its structure - approve the request above and I will take a look.',
    })
    await enqueueTutorResponse({
      raw: toolCallCompletion('list_workspace', { relative_path: '.' }),
    })
    await enqueueTutorResponse({
      content:
        'It is two files: main.py is the entry point, and parser.py holds the function to fill in.',
    })

    // Step 1: an ordinary message in the ordinary composer.
    await sendChatMessage(page, 'Read this starter project and explain how it is structured.')

    // The reply lands in the transcript, and the just-in-time access request appears
    // contextually above it - one compact card, the model's task-specific reason, and
    // the bounded dismissal. No setup dashboard, no separate agent view.
    await expect(
      page.getByText('I need to open the project folder before I can explain'),
    ).toBeVisible({ timeout: 30_000 })
    const accessCard = page.locator('[data-access-request="attach"]')
    await expect(accessCard).toBeVisible()
    await expect(
      accessCard.getByText('To inspect this starter project, Lyra needs to open the folder.'),
    ).toBeVisible()

    // Step 2: the student chooses the folder. The browser build has no native picker, so
    // the bounded path entry stands in - in the conversation surface, next to the card
    // that asked for it.
    await accessCard.getByRole('button', { name: 'Attach a folder' }).click()
    const pathEntry = page.getByLabel('Path to the folder')
    await expect(pathEntry).toBeVisible()
    await pathEntry.fill(workspaceDir)
    await page.getByRole('button', { name: 'Attach', exact: true }).click()

    // Step 3: the attached workspace becomes a compact chip in the composer's context row,
    // beside the source-context chip - the same weight as the material the answer reads.
    const chip = page.locator('[data-workspace-chip]')
    await expect(chip).toBeVisible({ timeout: 30_000 })
    await expect(chip).toContainText(`Workspace: ${basename(workspaceDir)}`)

    // Step 4: the turn that ended on the request continues itself: the student's own
    // question is re-answered with the access in hand, in the same transcript. The first
    // reply is superseded, not joined by a second answer.
    await expect(page.getByText('It is two files: main.py is the entry point')).toBeVisible({
      timeout: 30_000,
    })
    // The interrupted reply is superseded, not joined by a second answer: its waiting note
    // is gone from the transcript, replaced by the real answer in the same thread.
    await expect(page.getByText('approve the request above')).toBeHidden({ timeout: 30_000 })
  })

  test('reviews and applies an edit, then approves the exact verification command', async ({
    page,
  }) => {
    const session = await createSession(classId)
    await page.goto(`/classes/${classId}/chat?session=${session.id}`)
    await page.waitForLoadState('networkidle')

    // The conversation continues in the same composer. No destination change, no mode
    // choice: the agent decides that this turn needs the deeper grants first - the attached
    // folder carries only the read minimum - and then queues the edit and the run.
    const baseHash = createHash('sha256').update(PARSER_BASE).digest('hex')
    // The turn's model calls, in order: the two just-in-time asks, the answer they
    // produce, and - once the student approves both and the turn continues itself - the
    // change proposal, the command proposal, and the reply that names them.
    await enqueueTutorResponse({
      raw: toolCallCompletion('request_workspace_access', {
        scope: 'propose_changes',
        reason: 'To add the parser skeleton, Lyra needs to propose an edit to parser.py.',
      }),
    })
    await enqueueTutorResponse({
      raw: toolCallCompletion('request_workspace_access', {
        scope: 'run_commands',
        reason: 'To run the test, Lyra needs to run python3 test_parser.py in the project.',
      }),
    })
    await enqueueTutorResponse({
      content: 'Two grants are pending: the edit to parser.py, and running the test.',
    })
    await enqueueTutorResponse({
      raw: toolCallCompletion('create_workspace_change', {
        relative_path: 'parser.py',
        observed_base_hash: baseHash,
        proposed_content: PARSER_SKELETON,
        rationale: 'Add the parser skeleton.',
      }),
    })
    await enqueueTutorResponse({
      raw: toolCallCompletion('create_command_request', {
        argv: ['python3', 'test_parser.py'],
        relative_cwd: '.',
        reason: 'Verify the parser skeleton against its test.',
        expected_signal: 'tests passed',
        timeout_seconds: 60,
      }),
    })
    await enqueueTutorResponse({
      content: 'The skeleton is queued for your review, and the test run is ready to approve.',
    })

    await sendChatMessage(page, 'Add the parser skeleton and run the tests.')

    // The just-in-time asks appear contextually in the conversation, each carrying the
    // model's task-specific reason.
    const changeAsk = page.locator('[data-access-request="propose_changes"]')
    await expect(
      changeAsk.getByText(
        'To add the parser skeleton, Lyra needs to propose an edit to parser.py.',
      ),
    ).toBeVisible({ timeout: 30_000 })
    const commandAsk = page.locator('[data-access-request="run_commands"]')
    await expect(
      commandAsk.getByText(
        'To run the test, Lyra needs to run python3 test_parser.py in the project.',
      ),
    ).toBeVisible()

    // The student approves both; with every open request satisfied, the turn continues
    // itself in the same transcript.
    await changeAsk.getByRole('button', { name: 'Approve' }).click()
    await commandAsk.getByRole('button', { name: 'Approve' }).click()

    // The reply settles, and the consequences appear contextually in the conversation:
    // the change review and the command confirmation, nothing applied or run yet.
    await expect(page.getByText('The skeleton is queued for your review')).toBeVisible({
      timeout: 30_000,
    })

    const changeCard = page.locator('[aria-label="Workspace change for parser.py"]')
    await expect(changeCard).toBeVisible({ timeout: 30_000 })
    await expect(changeCard.getByText('Add the parser skeleton.')).toBeVisible()

    const commandCard = page.locator('[aria-label^="Command request"]')
    await expect(commandCard).toBeVisible({ timeout: 30_000 })
    await expect(commandCard.getByText('python3')).toBeVisible()
    await expect(
      commandCard.getByText('Verify the parser skeleton against its test.'),
    ).toBeVisible()

    // Nothing applies until the student does it: the file on disk is still the base.
    expect(await readFile(join(workspaceDir, 'parser.py'), 'utf-8')).toBe(PARSER_BASE)

    await changeCard.getByRole('button', { name: /Accept remaining/i }).click()
    await expect(changeCard.getByText('Applied')).toBeVisible({ timeout: 30_000 })
    expect(await readFile(join(workspaceDir, 'parser.py'), 'utf-8')).toBe(PARSER_SKELETON)

    // Nothing runs until the student confirms and runs the exact command.
    await commandCard.getByRole('button', { name: /Confirm and run/i }).click()
    await expect(commandCard.locator('[aria-label="Command output"]')).toContainText(
      'tests passed',
      { timeout: 30_000 },
    )
    await expect(commandCard.getByText('Exit 0')).toBeVisible()

    // The composer remains the one place to talk to Lyra, and it is usable again.
    const composer = page.locator('#message-composer')
    await expect(composer).toBeEnabled()
    await expect(page.locator('[aria-label="Send message"]')).toBeVisible()
  })

  test('"Not now" dismisses the access request for the session without granting anything', async ({
    page,
  }) => {
    const session = await createSession(classId)
    // The previous tests attached the workspace and earned its grants. The dismissal
    // story needs an open ask, so start from the unattached state: the same folder, no
    // workspace behind it.
    await apiDelete(`/api/classes/${classId}/workspace`)
    await page.goto(`/classes/${classId}/chat?session=${session.id}`)
    await page.waitForLoadState('networkidle')
    await enqueueTutorResponse({
      raw: toolCallCompletion('request_workspace_access', {
        scope: 'attach',
        reason: 'To inspect this starter project, Lyra needs to open the folder.',
      }),
    })
    await enqueueTutorResponse({
      content: 'I need the folder to look at the project - approve the request above when ready.',
    })

    await sendChatMessage(page, 'Read this starter project and explain how it is structured.')

    const accessCard = page.locator('[data-access-request="attach"]')
    await expect(
      accessCard.getByText('To inspect this starter project, Lyra needs to open the folder.'),
    ).toBeVisible({ timeout: 30_000 })

    // The bounded dismissal: the card goes away for the rest of the session. It is
    // server state, not component state - a reload or unmount cannot resurface it while
    // it is active, and nothing is granted on the way.
    await accessCard.getByRole('button', { name: 'Not now' }).click()
    await expect(accessCard).toHaveCount(0)

    const dismissalRes = await apiGet(
      `/api/classes/${classId}/sessions/${session.id}/agent/access-dismissals`,
    )
    expect(dismissalRes.status).toBe(200)
    const dismissals = (await dismissalRes.json()) as { dismissals: Array<{ scope: string }> }
    expect(dismissals.dismissals.map((d) => d.scope)).toContain('attach')

    const workspaceRes = await apiGet(`/api/classes/${classId}/workspace`)
    expect(workspaceRes.status).toBe(200)
    expect(await workspaceRes.json()).toBeNull()
  })
})
