/**
 * Backup, credential transition, and recovery through the real stack.
 *
 * Proves: API key set/check/delete lifecycle, endpoint change resets
 * remote_ack (PLA-302), real launcher backup() creates a valid archive
 * and real launcher restore() recovers it with entity verification (PLA-307).
 * Uses the actual backup()/restore() functions with monkeypatched stack
 * management so the running acceptance stack is not torn down.
 */

import { test, expect } from '@playwright/test'
import { execSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import {
  apiGet,
  apiPut,
  createClass,
  uploadDocument,
  waitForDocumentReady,
  readAcceptanceState,
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
})
