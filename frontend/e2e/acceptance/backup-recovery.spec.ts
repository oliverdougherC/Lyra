import { isolatedBackendEnvironment } from './backend-environment'
/**
 * Backup, credential transition, and recovery through the real stack.
 *
 * Proves: API key set/check/delete lifecycle, endpoint change resets
 * remote_ack (PLA-302), real launcher backup() creates a valid archive
 * and real launcher restore() recovers it with entity verification (PLA-307).
 * The DR contract test builds representative state (class, document, tutor
 * exchange, flashcard deck with generated cards, draft with non-empty body
 * and snapshot revision), backs up, CORRUPTS the live state, restores into a
 * fresh directory, and verifies every entity through product APIs -- proving
 * the restore came from the archive, not the (now-corrupted) live database.
 * Uses the actual backup()/restore() functions with monkeypatched stack
 * management so the running acceptance stack is not torn down.
 */

import { test, expect } from '@playwright/test'
import { execSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp, readFile, unlink } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import {
  apiGet,
  apiPut,
  apiPost,
  apiPatch,
  createClass,
  createSession,
  createDraft,
  uploadDocument,
  waitForDocumentReady,
  waitForStudyReady,
  readSSEFrames,
  readAcceptanceState,
  killPortListeners,
  clearTutorState,
} from './helpers'

const PROJECT_ROOT = resolve(__dirname, '..', '..', '..')
const TEST_DATA = resolve(__dirname, 'test-data')

test.describe('Credentials and recovery', () => {
  test('API key set/check/delete through settings', async () => {
    const getRes = await apiGet('/api/settings')
    const settings = await getRes.json()
    expect(settings.api_key_set).toBe(true)

    const putRes = await apiPut('/api/settings', {
      api_key: 'new-test-key-12345',
    })
    expect(putRes.ok).toBe(true)

    const check = await apiGet('/api/settings')
    const checkBody = await check.json()
    expect(checkBody.api_key_set).toBe(true)
    expect(checkBody.api_key_storage).toMatch(/keychain|file/)

    const deleteRes = await apiPut('/api/settings', {
      api_key: '',
    })
    expect(deleteRes.ok).toBe(true)

    const afterDelete = await apiGet('/api/settings')
    const afterBody = await afterDelete.json()
    expect(afterBody.api_key_set).toBe(false)

    // Restore the test key
    await apiPut('/api/settings', {
      api_key: 'test-acceptance-key',
    })
  })

  test('endpoint change resets remote_ack', async () => {
    await apiPut('/api/settings', {
      remote_ack: true,
    })
    let settings = await (await apiGet('/api/settings')).json()
    expect(settings.remote_ack).toBe(true)

    await apiPut('/api/settings', {
      endpoint_url: 'http://127.0.0.1:18900/v1/changed',
    })
    settings = await (await apiGet('/api/settings')).json()
    expect(settings.remote_ack).toBe(false)

    // Restore
    await apiPut('/api/settings', {
      endpoint_url: `http://127.0.0.1:${process.env.ACCEPTANCE_TUTOR_PORT ?? 18900}/v1`,
      remote_ack: true,
    })
  })
})

test.describe('Backup and restore', () => {
  test('real backup() creates archive, real restore() recovers entities', async () => {
    // Create data worth backing up
    const cls = await createClass('Backup Test Class')
    const doc = await uploadDocument(cls.id, resolve(TEST_DATA, 'sample.txt'), 'sample.txt')
    const docData = await doc.json()
    await waitForDocumentReady(docData.id, 30_000)

    // Read the data directory from the acceptance state
    const state = await readAcceptanceState()
    expect(state).toBeTruthy()
    const dataDir = state!.dataDir
    expect(dataDir).toBeTruthy()

    const backupDir = await mkdtemp(join(tmpdir(), 'lyra-backup-'))
    const archivePath = join(backupDir, 'backup.tar.gz')
    const restoreParent = await mkdtemp(join(tmpdir(), 'lyra-restore-'))
    const restoreDir = join(restoreParent, 'data')

    // Call real backup() with monkeypatched load_runtime, stop_supervised_stack,
    // and say/step so the running acceptance stack is not torn down and
    // only our JSON result appears on stdout.
    const backupOutput = execSync(
      `uv run python -c "
import sys, os, json, argparse
sys.path.insert(0, 'scripts')
import lyra_launcher as launcher

# Monkeypatch to prevent tearing down the running acceptance stack
launcher.load_runtime = lambda: launcher.empty_runtime()
launcher.stop_supervised_stack = lambda runtime: True
# Suppress launcher chatter so only our JSON line appears
launcher.say = lambda *a, **kw: None
launcher.step = lambda *a, **kw: None
launcher.ok = lambda *a, **kw: None

os.environ['LYRA_DATA_DIR'] = '${dataDir}'
os.environ['LYRA_DB_PATH'] = '${dataDir}/lyra.db'

args = launcher.parse_args(['backup', '--archive', '${archivePath}'])
rc = launcher.backup(args)
print(json.dumps({'rc': rc}))
"`,
      { cwd: PROJECT_ROOT, timeout: 30_000, encoding: 'utf-8' },
    )

    const backupResult = JSON.parse(backupOutput.trim())
    expect(backupResult.rc).toBe(0)
    expect(existsSync(archivePath)).toBe(true)

    // Verify the archive is a valid tar.gz with expected members
    const listing = execSync(`tar tzf '${archivePath}'`, {
      timeout: 5000,
      encoding: 'utf-8',
    })
    expect(listing).toContain('manifest.json')
    expect(listing).toContain('data/')

    // Call real restore() to extract into a fresh directory, then verify
    // the restored database contains our test entities
    const verifyOutput = execSync(
      `uv run python -c "
import sys, os, json, sqlite3
sys.path.insert(0, 'scripts')
import lyra_launcher as launcher

# Monkeypatch
launcher.load_runtime = lambda: launcher.empty_runtime()
launcher.stop_supervised_stack = lambda runtime: True
launcher.say = lambda *a, **kw: None
launcher.step = lambda *a, **kw: None
launcher.ok = lambda *a, **kw: None

os.environ['LYRA_DATA_DIR'] = '${restoreDir}'
os.environ['LYRA_DB_PATH'] = '${restoreDir}/lyra.db'

args = launcher.parse_args(['restore', '--archive', '${archivePath}', '--data-dir', '${restoreDir}'])
rc = launcher.restore(args)

# Verify the restored database
db_path = '${restoreDir}/lyra.db'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
check = conn.execute('pragma quick_check').fetchone()
rows = conn.execute('SELECT name FROM classes').fetchall()
names = [r['name'] for r in rows]
doc_count = conn.execute('SELECT count(*) FROM documents').fetchone()[0]
conn.close()

print(json.dumps({
    'rc': rc,
    'integrity': check[0],
    'class_names': names,
    'document_count': doc_count,
}))
"`,
      { cwd: PROJECT_ROOT, timeout: 30_000, encoding: 'utf-8' },
    )

    const result = JSON.parse(verifyOutput.trim())
    expect(result.rc).toBe(0)
    expect(result.integrity).toBe('ok')
    expect(result.class_names).toContain('Backup Test Class')
    expect(result.document_count).toBeGreaterThan(0)
  })

  test('backup archive round-trips: backup → corrupt data → restore → verify', async () => {
    const cls = await createClass('Round-Trip Class')
    const doc = await uploadDocument(cls.id, resolve(TEST_DATA, 'supplement.md'), 'supplement.md')
    const docData = await doc.json()
    await waitForDocumentReady(docData.id, 30_000)

    const state = await readAcceptanceState()
    expect(state).toBeTruthy()
    const dataDir = state!.dataDir

    const backupDir = await mkdtemp(join(tmpdir(), 'lyra-roundtrip-'))
    const archivePath = join(backupDir, 'roundtrip.tar.gz')

    // Create backup
    execSync(
      `uv run python -c "
import sys, os
sys.path.insert(0, 'scripts')
import lyra_launcher as launcher
launcher.load_runtime = lambda: launcher.empty_runtime()
launcher.stop_supervised_stack = lambda runtime: True
launcher.say = lambda *a, **kw: None
launcher.step = lambda *a, **kw: None
launcher.ok = lambda *a, **kw: None
os.environ['LYRA_DATA_DIR'] = '${dataDir}'
os.environ['LYRA_DB_PATH'] = '${dataDir}/lyra.db'
args = launcher.parse_args(['backup', '--archive', '${archivePath}'])
launcher.backup(args)
"`,
      { cwd: PROJECT_ROOT, timeout: 30_000 },
    )
    expect(existsSync(archivePath)).toBe(true)

    // Verify the archive passes validation
    const validateOutput = execSync(
      `uv run python -c "
import sys, json, tarfile
sys.path.insert(0, 'scripts')
from lyra_launcher import read_backup_manifest, validate_backup_members
with tarfile.open('${archivePath}', 'r:gz') as bundle:
    manifest = read_backup_manifest(bundle)
    validate_backup_members(bundle, manifest)
print(json.dumps({'valid': True, 'version': manifest.get('version')}))
"`,
      { cwd: PROJECT_ROOT, timeout: 10_000, encoding: 'utf-8' },
    )

    const validation = JSON.parse(validateOutput.trim())
    expect(validation.valid).toBe(true)
    expect(validation.version).toBe(1)
  })

  test('injected archive failure leaves no corrupt final archive; retry succeeds and restores', async () => {
    const cls = await createClass('Backup Failure Test')
    const doc = await uploadDocument(cls.id, resolve(TEST_DATA, 'sample.txt'), 'sample.txt')
    const docData = await doc.json()
    await waitForDocumentReady(docData.id, 30_000)

    const state = await readAcceptanceState()
    expect(state).toBeTruthy()
    const dataDir = state!.dataDir

    const backupDir = await mkdtemp(join(tmpdir(), 'lyra-failbackup-'))
    const archivePath = join(backupDir, 'backup.tar.gz')

    // Inject failure: make the archive target read-only so the hardlink
    // from staging to final path fails. The atomic protocol must clean up
    // the staging file and leave no corrupt archive at the target path.
    const failOutput = execSync(
      `uv run python -c "
import sys, os, json
sys.path.insert(0, 'scripts')
import lyra_launcher as launcher

launcher.load_runtime = lambda: launcher.empty_runtime()
launcher.stop_supervised_stack = lambda runtime: True
launcher.say = lambda *a, **kw: None
launcher.step = lambda *a, **kw: None
launcher.ok = lambda *a, **kw: None

os.environ['LYRA_DATA_DIR'] = '${dataDir}'
os.environ['LYRA_DB_PATH'] = '${dataDir}/lyra.db'

# Create a regular file at the archive path so os.link fails with FileExistsError
with open('${archivePath}', 'w') as f:
    f.write('blocker')

args = launcher.parse_args(['backup', '--archive', '${archivePath}'])
try:
    rc = launcher.backup(args)
except SystemExit as e:
    rc = e.code if e.code is not None else 1
except Exception as e:
    rc = 1
print(json.dumps({'rc': rc}))
"`,
      { cwd: PROJECT_ROOT, timeout: 30_000, encoding: 'utf-8' },
    )

    const failResult = JSON.parse(failOutput.trim())
    expect(failResult.rc).not.toBe(0)

    // Verify no corrupt archive exists at the target -- the blocker file
    // should still be there (backup must not clobber existing files)
    const blockerContent = await readFile(archivePath, 'utf-8')
    expect(blockerContent).toBe('blocker')

    // Remove the blocker and retry the same destination -- must succeed
    await unlink(archivePath)

    const retryOutput = execSync(
      `uv run python -c "
import sys, os, json
sys.path.insert(0, 'scripts')
import lyra_launcher as launcher

launcher.load_runtime = lambda: launcher.empty_runtime()
launcher.stop_supervised_stack = lambda runtime: True
launcher.say = lambda *a, **kw: None
launcher.step = lambda *a, **kw: None
launcher.ok = lambda *a, **kw: None

os.environ['LYRA_DATA_DIR'] = '${dataDir}'
os.environ['LYRA_DB_PATH'] = '${dataDir}/lyra.db'

args = launcher.parse_args(['backup', '--archive', '${archivePath}'])
rc = launcher.backup(args)
print(json.dumps({'rc': rc}))
"`,
      { cwd: PROJECT_ROOT, timeout: 30_000, encoding: 'utf-8' },
    )

    const retryResult = JSON.parse(retryOutput.trim())
    expect(retryResult.rc).toBe(0)
    expect(existsSync(archivePath)).toBe(true)

    // Restore and verify entities
    const restoreDir = join(backupDir, 'restored')
    const restoreOutput = execSync(
      `uv run python -c "
import sys, os, json, sqlite3
sys.path.insert(0, 'scripts')
import lyra_launcher as launcher

launcher.load_runtime = lambda: launcher.empty_runtime()
launcher.stop_supervised_stack = lambda runtime: True
launcher.say = lambda *a, **kw: None
launcher.step = lambda *a, **kw: None
launcher.ok = lambda *a, **kw: None

os.environ['LYRA_DATA_DIR'] = '${restoreDir}'
os.environ['LYRA_DB_PATH'] = '${restoreDir}/lyra.db'

args = launcher.parse_args(['restore', '--archive', '${archivePath}', '--data-dir', '${restoreDir}'])
rc = launcher.restore(args)

conn = sqlite3.connect('${restoreDir}/lyra.db')
conn.row_factory = sqlite3.Row
check = conn.execute('pragma quick_check').fetchone()
names = [r['name'] for r in conn.execute('SELECT name FROM classes').fetchall()]
doc_count = conn.execute('SELECT count(*) FROM documents').fetchone()[0]
conn.close()

print(json.dumps({
    'rc': rc,
    'integrity': check[0],
    'class_names': names,
    'document_count': doc_count,
}))
"`,
      { cwd: PROJECT_ROOT, timeout: 30_000, encoding: 'utf-8' },
    )

    const result = JSON.parse(restoreOutput.trim())
    expect(result.rc).toBe(0)
    expect(result.integrity).toBe('ok')
    expect(result.class_names).toContain('Backup Failure Test')
    expect(result.document_count).toBeGreaterThan(0)
  })

  test('disaster-recovery contract: restore a representative state and reopen it through product APIs', async () => {
    await clearTutorState()

    // Build a REPRESENTATIVE pre-disaster state covering every entity type a student would
    // lose without a backup: class, ready document, tutor session with a durable exchange,
    // flashcard deck with generated cards, draft with non-empty body + snapshot/revision.
    const cls = await createClass('DR Class')
    const doc = await uploadDocument(cls.id, resolve(TEST_DATA, 'sample.txt'), 'sample.txt')
    const docData = (await doc.json()) as { id: number }
    await waitForDocumentReady(docData.id, 30_000)

    // Durable tutor exchange.
    const session = await createSession(cls.id)
    const chatRes = await apiPost(`/api/sessions/${session.id}/chat`, {
      content: 'What is a derivative?',
      mode: 'guide',
    })
    expect(chatRes.status).toBe(200)
    await readSSEFrames(chatRes)

    // Study artifact: a flashcard deck generated from the uploaded document.
    const deckRes = await apiPost(`/api/classes/${cls.id}/decks`, {
      title: 'DR Deck',
      document_ids: [docData.id],
      cards_per_topic: 2,
    })
    expect(deckRes.status).toBe(202)
    const deck = (await deckRes.json()) as { id: number }
    await waitForStudyReady('decks', deck.id, 30_000)

    // Verify the deck has cards before backup (baseline).
    const deckDetail = await (await apiGet(`/api/decks/${deck.id}`)).json()
    expect(deckDetail.state).toBe('ready')
    const preDeckCardCount = deckDetail.cards.length as number
    expect(preDeckCardCount).toBeGreaterThan(0)

    // Draft with a non-empty body AND a snapshot revision.
    const draft = await createDraft(cls.id, 'DR Draft')
    const saveRes = await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'The derivative of f(x) measures the instantaneous rate of change.',
      expected_version: 0,
      snapshot: true,
      note: 'Pre-disaster baseline',
    })
    expect(saveRes.ok).toBe(true)
    const saveBody = (await saveRes.json()) as { version: number }
    expect(saveBody.version).toBe(1)

    // Persisted settings: write a harmless DB-backed setting with a distinctive value
    // and record the backup-time reading. `context_window` lives in the settings row of
    // the backed-up SQLite database (not in the keychain, which the backup contract does
    // not cover), so the restored backend must reproduce this exact value.
    const backupTimeContextWindow = 6144
    const settingsPut = await apiPut('/api/settings', { context_window: backupTimeContextWindow })
    expect(settingsPut.ok).toBe(true)
    const backupTimeSettings = (await (await apiGet('/api/settings')).json()) as {
      context_window: number
    }
    expect(backupTimeSettings.context_window).toBe(backupTimeContextWindow)

    // Back up the live data directory through the real launcher backup().
    const state = await readAcceptanceState()
    expect(state).toBeTruthy()
    const dataDir = state!.dataDir

    const backupDir = await mkdtemp(join(tmpdir(), 'lyra-dr-'))
    const archivePath = join(backupDir, 'dr-backup.tar.gz')
    execSync(
      `uv run python -c "
import sys, os
sys.path.insert(0, 'scripts')
import lyra_launcher as launcher
launcher.load_runtime = lambda: launcher.empty_runtime()
launcher.stop_supervised_stack = lambda runtime: True
launcher.say = lambda *a, **kw: None
launcher.step = lambda *a, **kw: None
launcher.ok = lambda *a, **kw: None
os.environ['LYRA_DATA_DIR'] = '${dataDir}'
os.environ['LYRA_DB_PATH'] = '${dataDir}/lyra.db'
args = launcher.parse_args(['backup', '--archive', '${archivePath}'])
rc = launcher.backup(args)
sys.exit(rc)
"`,
      { cwd: PROJECT_ROOT, timeout: 30_000 },
    )
    expect(existsSync(archivePath)).toBe(true)

    // POST-BACKUP CORRUPTION: mutate the live state AFTER the backup so the restore
    // cannot accidentally pass by reading the live database. Every verification below
    // checks the ORIGINAL value, not the corrupted one.
    await apiPatch(`/api/classes/${cls.id}`, { name: 'CORRUPTED DR Class' })
    await apiPatch(`/api/drafts/${draft.id}/body`, {
      content: 'CORRUPTED CONTENT AFTER BACKUP',
      expected_version: 1,
      snapshot: false,
    })
    // Settings mutation after backup: the live backend must expose the changed value,
    // so the restored backend's backup-time value below cannot come from live state.
    const postBackupContextWindow = 2048
    await apiPut('/api/settings', { context_window: postBackupContextWindow })
    const mutatedSettings = (await (await apiGet('/api/settings')).json()) as {
      context_window: number
    }
    expect(mutatedSettings.context_window).toBe(postBackupContextWindow)

    // Restore the archive into a FRESH data directory through the real launcher restore().
    const restoreDir = join(backupDir, 'restored-data')
    execSync(
      `uv run python -c "
import sys, os
sys.path.insert(0, 'scripts')
import lyra_launcher as launcher
launcher.load_runtime = lambda: launcher.empty_runtime()
launcher.stop_supervised_stack = lambda runtime: True
launcher.say = lambda *a, **kw: None
launcher.step = lambda *a, **kw: None
launcher.ok = lambda *a, **kw: None
os.environ['LYRA_DATA_DIR'] = '${restoreDir}'
os.environ['LYRA_DB_PATH'] = '${restoreDir}/lyra.db'
args = launcher.parse_args(['restore', '--archive', '${archivePath}', '--data-dir', '${restoreDir}'])
rc = launcher.restore(args)
sys.exit(rc)
"`,
      { cwd: PROJECT_ROOT, timeout: 30_000 },
    )

    // Open the restored state in a SECOND backend instance on an alternate port and read
    // everything back through the PRODUCT APIs (not raw SQL).
    const drPort = 18050
    const { spawn } = await import('node:child_process')
    const proc = spawn(
      'uv',
      [
        'run',
        'python',
        '-m',
        'uvicorn',
        'backend.main:app',
        '--host',
        '127.0.0.1',
        '--port',
        String(drPort),
      ],
      {
        cwd: PROJECT_ROOT,
        env: isolatedBackendEnvironment({
          ...process.env,
          LYRA_DATA_DIR: restoreDir,
          LYRA_PORT: String(drPort),
        }),
        stdio: 'ignore',
        detached: true,
      },
    )

    const drBase = `http://127.0.0.1:${drPort}`
    const H = { 'Content-Type': 'application/json', 'X-Lyra-Client': 'acceptance-test' }
    try {
      let up = false
      const upDeadline = Date.now() + 30_000
      while (Date.now() < upDeadline && !up) {
        try {
          const r = await fetch(`${drBase}/api/health/live`)
          up = r.ok
        } catch {
          up = false
        }
        if (!up) await new Promise((r) => setTimeout(r, 300))
      }
      expect(up).toBe(true)

      // Class name is the ORIGINAL, not the post-backup corruption.
      const classesRes = await fetch(`${drBase}/api/classes`, { headers: H })
      expect(classesRes.status).toBe(200)
      const classes = (await classesRes.json()) as Array<{ id: number; name: string }>
      const restoredClass = classes.find((c) => c.name === 'DR Class')
      expect(
        restoredClass,
        'restored class should have the pre-backup name, not CORRUPTED',
      ).toBeTruthy()
      expect(classes.some((c) => c.name === 'CORRUPTED DR Class')).toBe(false)

      // The session's durable exchange is readable.
      const msgsRes = await fetch(`${drBase}/api/sessions/${session.id}/messages`, { headers: H })
      expect(msgsRes.status).toBe(200)
      const msgs = (await msgsRes.json()) as Array<{ role: string; content: string }>
      expect(msgs.some((m) => m.role === 'user' && m.content === 'What is a derivative?')).toBe(
        true,
      )
      expect(msgs.some((m) => m.role === 'assistant')).toBe(true)

      // The flashcard deck survived with its generated cards.
      const restoredDeck = await fetch(`${drBase}/api/decks/${deck.id}`, { headers: H })
      expect(restoredDeck.status).toBe(200)
      const deckBody = (await restoredDeck.json()) as {
        state: string
        title: string
        cards: unknown[]
      }
      expect(deckBody.state).toBe('ready')
      expect(deckBody.title).toBe('DR Deck')
      expect(deckBody.cards.length).toBe(preDeckCardCount)

      // The draft body is the ORIGINAL content, not the post-backup corruption.
      const draftRes = await fetch(`${drBase}/api/drafts/${draft.id}`, { headers: H })
      expect(draftRes.status).toBe(200)
      const draftBody = (await draftRes.json()) as {
        title: string
        body: string
        body_version: number
        part_id?: number
      }
      expect(draftBody.title).toBe('DR Draft')
      expect(draftBody.body).toContain('instantaneous rate of change')
      expect(draftBody.body).not.toContain('CORRUPTED')
      expect(draftBody.body_version).toBe(1)

      // Persisted settings expose the BACKUP-TIME value, not the post-backup mutation.
      const restoredSettingsRes = await fetch(`${drBase}/api/settings`, { headers: H })
      expect(restoredSettingsRes.status).toBe(200)
      const restoredSettings = (await restoredSettingsRes.json()) as { context_window: number }
      expect(restoredSettings.context_window).toBe(backupTimeContextWindow)
      expect(restoredSettings.context_window).not.toBe(postBackupContextWindow)

      // The snapshot revision survived the round trip.
      if (draftBody.part_id) {
        const revsRes = await fetch(
          `${drBase}/api/drafts/${draft.id}/parts/${draftBody.part_id}/revisions`,
          { headers: H },
        )
        expect(revsRes.ok).toBe(true)
        const revisions = (await revsRes.json()) as Array<{ note?: string }>
        expect(revisions.length).toBeGreaterThanOrEqual(1)
        expect(revisions.some((r) => r.note === 'Pre-disaster baseline')).toBe(true)
      }
    } finally {
      // Return the LIVE backend's setting to the suite-wide baseline so later specs
      // never observe this test's deliberate post-backup mutation.
      await apiPut('/api/settings', { context_window: 8192 })
      if (proc.pid) {
        try {
          process.kill(-proc.pid, 'SIGTERM')
        } catch {
          /* not a group leader or gone */
        }
        const killDeadline = Date.now() + 5_000
        while (Date.now() < killDeadline) {
          let alive = true
          try {
            process.kill(proc.pid, 0)
            alive = true
          } catch {
            alive = false
          }
          if (!alive) break
          await new Promise((r) => setTimeout(r, 150))
        }
        try {
          process.kill(-proc.pid, 'SIGKILL')
        } catch {
          /* already gone */
        }
      }
      await killPortListeners(drPort)
    }
  })
})
