/** Shared display formatters. Copy rules live in docs/ui-phase-1.md. */

const RELATIVE_UNITS: [limitSeconds: number, perUnit: number, unit: Intl.RelativeTimeFormatUnit][] =
  [
    [60, 1, 'second'],
    [3600, 60, 'minute'],
    [86400, 3600, 'hour'],
    [604800, 86400, 'day'],
    [2629800, 604800, 'week'],
    [31557600, 2629800, 'month'],
    [Number.POSITIVE_INFINITY, 31557600, 'year'],
  ]

const relative = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })

/**
 * SQLite writes `datetime('now')` as a naive UTC string. Parsing it without the `Z` would
 * be read as local time and show every timestamp hours off.
 */
export function parseTimestamp(value: string): Date {
  return new Date(/[Z+]|-\d\d:\d\d$/.test(value) ? value : `${value.replace(' ', 'T')}Z`)
}

export function formatRelativeTime(value: string): string {
  const elapsedSeconds = (parseTimestamp(value).getTime() - Date.now()) / 1000
  const magnitude = Math.abs(elapsedSeconds)
  for (const [limit, perUnit, unit] of RELATIVE_UNITS) {
    if (magnitude < limit) return relative.format(Math.round(elapsedSeconds / perUnit), unit)
  }
  return relative.format(Math.round(elapsedSeconds / 31557600), 'year')
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function formatCount(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`
}

/**
 * Truncates from the middle so the extension stays visible, which is what tells the user
 * what kind of file a row is.
 */
export function truncateMiddle(value: string, max = 32): string {
  if (value.length <= max) return value
  const head = Math.ceil((max - 1) / 2)
  return `${value.slice(0, head)}...${value.slice(value.length - (max - 1 - head))}`
}

/** Two-character avatar label, from the course code when there is one. */
export function initialsFor(name: string, code: string | null): string {
  const source = code?.trim() || name.trim()
  const words = source.split(/\s+/).filter(Boolean)
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase()
  return source.slice(0, 2).toUpperCase()
}
