import { execSync } from 'node:child_process'

/** Select fixture processes only from the process tree or groups this run owns. */
export function ownedFixturePids(listing: string, roots: number[]): number[] {
  const rows = listing.split('\n').flatMap((line) => {
    const match = line.trim().match(/^(\d+)\s+(\d+)\s+(\d+)\s+(.*)$/)
    return match
      ? [
          {
            pid: Number(match[1]),
            parent: Number(match[2]),
            group: Number(match[3]),
            command: match[4],
          },
        ]
      : []
  })
  const owned = new Set(roots)
  let previousSize = -1
  while (previousSize !== owned.size) {
    previousSize = owned.size
    for (const row of rows) {
      if (owned.has(row.parent) || roots.includes(row.group)) owned.add(row.pid)
    }
  }
  return rows
    .filter(
      (row) =>
        owned.has(row.pid) &&
        ['acceptance.backend_harness:app', 'e2e/acceptance/fake-helper.py'].some((pattern) =>
          row.command.includes(pattern),
        ),
    )
    .map((row) => row.pid)
}

function processBirthToken(pid: number): string | null {
  try {
    return execSync(`ps -p ${pid} -o lstart=`, { encoding: 'utf-8', timeout: 2000 }).trim() || null
  } catch {
    return null
  }
}

/** Capture detached descendants before stopping their parent erases the ancestry. */
export function captureOwnedFixtures(roots: number[]): Map<number, string> {
  const owned = new Map<number, string>()
  try {
    const listing = execSync('ps -axww -o pid=,ppid=,pgid=,command=', {
      encoding: 'utf-8',
      timeout: 5000,
    })
    for (const pid of ownedFixturePids(listing, roots)) {
      const token = processBirthToken(pid)
      if (token) owned.set(pid, token)
    }
  } catch {
    /* port and group checks still report unreclaimed processes */
  }
  return owned
}

/** A recycled PID is never ownership evidence, even if its command looks identical. */
export function survivingOwnedFixtures(
  owned: Map<number, string>,
  readToken: (pid: number) => string | null = processBirthToken,
): number[] {
  return [...owned].filter(([pid, token]) => readToken(pid) === token).map(([pid]) => pid)
}
