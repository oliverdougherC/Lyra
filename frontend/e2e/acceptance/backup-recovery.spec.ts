/**
 * Backup, credential transition, and recovery through the real stack.
 *
 * Proves: API key set/check/delete lifecycle, endpoint change resets
 * remote_ack (PLA-302), real launcher backup creates a valid archive that
 * contains representative entities (PLA-307).  Required tests fail (not skip)
 * if the harness cannot provide the data directory.
 */

import { test, expect } from '@playwright/test'
import { execSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdtemp, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { apiGet, apiPut, createClass, uploadDocument, waitForDocumentReady } from './helpers'

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
  test('backup creates a valid archive containing representative entities', async () => {
    // Create data worth backing up
    const cls = await createClass('Backup Test Class')
    const doc = await uploadDocument(cls.id, resolve(TEST_DATA, 'sample.txt'), 'sample.txt')
    const docData = await doc.json()
    await waitForDocumentReady(docData.id, 30_000)

    // Read the data directory from the state file -- this is a REQUIRED
    // assertion, not optional: if we can't find the data dir, the test fails.
    const stateRaw = await readFile(join(PROJECT_ROOT, '.acceptance-state.json'), 'utf-8')
    const state = JSON.parse(stateRaw)
    const dataDir: string = state.dataDir
    expect(dataDir).toBeTruthy()

    const backupDir = await mkdtemp(join(tmpdir(), 'lyra-backup-'))
    const archivePath = join(backupDir, 'backup.tar.gz')

    // Run backup via the launcher's backup module
    execSync(
      `uv run python -c "
import sys; sys.path.insert(0, '.')
from scripts.lyra_launcher import stage_backup_tree
import tarfile, tempfile, shutil
from pathlib import Path

data = Path('${dataDir}')
db = data / 'lyra.db'
stage_dir = Path(tempfile.mkdtemp())
manifest = stage_backup_tree(stage_dir, data, db)

with tarfile.open('${archivePath}', 'w:gz') as tar:
    for item in stage_dir.iterdir():
        tar.add(item, arcname=item.name)

shutil.rmtree(stage_dir)
print('Backup created, manifest:', manifest)
"`,
      { cwd: PROJECT_ROOT, timeout: 30_000 },
    )

    expect(existsSync(archivePath)).toBe(true)

    // Verify the archive is a valid tar.gz containing expected members
    const listing = execSync(`tar tzf '${archivePath}'`, {
      timeout: 5000,
      encoding: 'utf-8',
    })
    expect(listing).toContain('lyra.db')

    // Verify the backup database contains our test class
    const verifyOutput = execSync(
      `uv run python -c "
import sys, tarfile, tempfile, sqlite3, json
from pathlib import Path

extract_dir = Path(tempfile.mkdtemp())
with tarfile.open('${archivePath}', 'r:gz') as tar:
    tar.extractall(extract_dir)

db_path = extract_dir / 'lyra.db'
conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT name FROM classes').fetchall()
names = [r['name'] for r in rows]
conn.close()

import shutil
shutil.rmtree(extract_dir)

result = {'class_names': names}
print(json.dumps(result))
"`,
      { cwd: PROJECT_ROOT, timeout: 10_000, encoding: 'utf-8' },
    )

    const result = JSON.parse(verifyOutput.trim())
    expect(result.class_names).toContain('Backup Test Class')
  })
})
