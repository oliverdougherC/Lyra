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

const shortDate = new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric' })

/**
 * Fallback name for a conversation the backend has not titled yet, which in practice
 * means one with no messages. Dated rather than numbered: a positional name renumbers
 * every time a conversation is added or removed, so the same chat keeps changing name.
 */
export function formatSessionFallbackTitle(createdAt: string): string {
  return `Chat from ${shortDate.format(parseTimestamp(createdAt))}`
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
  // The ellipsis costs three characters, so the budget for real text is `max - 3`. Reserving
  // one made every result exactly two characters longer than the caller asked for.
  const head = Math.max(0, Math.ceil((max - 3) / 2))
  const tail = Math.max(0, max - 3 - head)
  return `${value.slice(0, head)}...${tail === 0 ? '' : value.slice(value.length - tail)}`
}

/**
 * Short course mark, at most three characters.
 *
 * The subject prefix of a course code is what a student actually reads a class by, so
 * `ECE 203` marks as `ECE`. Taking the first letter of each word instead would render it
 * `E2`, which identifies nothing and collides with every other course in the department.
 * Classes with no code fall back to initials from the name.
 */
export function initialsFor(name: string, code: string | null): string {
  const trimmedCode = code?.trim() ?? ''
  if (trimmedCode) {
    const letters = /^[^\W\d_]+/u.exec(trimmedCode)?.[0]
    if (letters) return letters.slice(0, 3).toUpperCase()
    return trimmedCode.replace(/\s+/g, '').slice(0, 3).toUpperCase()
  }

  const words = name.trim().split(/\s+/).filter(Boolean)
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase()
  return name.trim().slice(0, 2).toUpperCase()
}
