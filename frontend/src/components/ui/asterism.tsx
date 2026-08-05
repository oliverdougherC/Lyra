import { cn } from '@/lib/utils'

/**
 * The house fleuron: three four-point stars, the middle one raised — a printer's asterism
 * drawn from the same concave star as the Lyra mark. It opens empty states and section
 * breaks the way a fleuron opens a chapter, and because it recurs, it reads as identity
 * rather than decoration.
 *
 * Everything is `currentColor`; callers set tone with a text color.
 */

/** A concave four-point star: cusps pulled to `waist` so the arms read as light. */
function starPath(cx: number, cy: number, radius: number, waist: number): string {
  return [
    `M${cx} ${cy - radius}`,
    `C${cx} ${cy - waist} ${cx + waist} ${cy} ${cx + radius} ${cy}`,
    `C${cx + waist} ${cy} ${cx} ${cy + waist} ${cx} ${cy + radius}`,
    `C${cx} ${cy + waist} ${cx - waist} ${cy} ${cx - radius} ${cy}`,
    `C${cx - waist} ${cy} ${cx} ${cy - waist} ${cx} ${cy - radius}`,
    'Z',
  ].join('')
}

export function Asterism({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 44 16" aria-hidden className={cn('h-4 w-11', className)}>
      <path d={starPath(8, 10, 3, 0.8)} fill="currentColor" opacity="0.55" />
      <path d={starPath(22, 6, 4, 1)} fill="currentColor" />
      <path d={starPath(36, 10, 3, 0.8)} fill="currentColor" opacity="0.55" />
    </svg>
  )
}
