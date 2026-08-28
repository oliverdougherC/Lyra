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

    // Run backup via the launcher's stage_backup_tree + archive creation --
    // the same sequence the real backup() handler uses, without the server
    // stop that would tear down the acceptance stack.
    execSync(
      `uv run python -c "
import sys; sys.path.insert(0, 'scripts')
from lyra_launcher import stage_backup_tree, read_backup_manifest, validate_backup_members, BACKUP_MANIFEST, BACKUP_DATA_PREFIX
import tarfile, tempfile, shutil, json
from pathlib import Path

data = Path('${dataDir}')
db = data / 'lyra.db'
stage_dir = Path(tempfile.mkdtemp())
manifest = stage_backup_tree(stage_dir, data, db)

# Create the archive the same way the real backup() does: pax format,
# manifest first, then the data prefix tree.
with tarfile.open('${archivePath}', 'w:gz', format=tarfile.PAX_FORMAT) as tar:
    tar.add(stage_dir / BACKUP_MANIFEST, arcname=BACKUP_MANIFEST)
    tar.add(stage_dir / BACKUP_DATA_PREFIX, arcname=BACKUP_DATA_PREFIX)

# Validate the written archive using the real verification functions
with tarfile.open('${archivePath}', 'r:gz') as bundle:
    read_manifest = read_backup_manifest(bundle)
    validate_backup_members(bundle, read_manifest)

shutil.rmtree(stage_dir)
print(json.dumps({'manifest': manifest, 'valid': True}))
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
    expect(listing).toContain('data/')

    // Extract and verify the backup database contains our test class,
    // then simulate restore by extracting into a fresh directory and
    // running SQLite quick_check (the same integrity check restore() uses).
    const verifyOutput = execSync(
      `uv run python -c "
import sys; sys.path.insert(0, 'scripts')
from lyra_launcher import read_backup_manifest, validate_backup_members, extract_archive_prefix, BACKUP_DATA_PREFIX
import tarfile, tempfile, sqlite3, json, shutil
from pathlib import Path

restore_dir = Path(tempfile.mkdtemp())

with tarfile.open('${archivePath}', 'r:gz') as bundle:
    manifest = read_backup_manifest(bundle)
    validate_backup_members(bundle, manifest)
    extract_archive_prefix(bundle, prefix=BACKUP_DATA_PREFIX, destination=restore_dir)

db_path = restore_dir / 'lyra.db'
conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row
# Same integrity check that restore() runs
check = conn.execute('pragma quick_check').fetchone()
rows = conn.execute('SELECT name FROM classes').fetchall()
names = [r['name'] for r in rows]
doc_count = conn.execute('SELECT count(*) FROM documents').fetchone()[0]
conn.close()
shutil.rmtree(restore_dir)

result = {
    'integrity': check[0],
    'class_names': names,
    'document_count': doc_count,
}
print(json.dumps(result))
"`,
      { cwd: PROJECT_ROOT, timeout: 10_000, encoding: 'utf-8' },
    )

    const result = JSON.parse(verifyOutput.trim())
    expect(result.integrity).toBe('ok')
    expect(result.class_names).toContain('Backup Test Class')
    expect(result.document_count).toBeGreaterThan(0)
  })
})
