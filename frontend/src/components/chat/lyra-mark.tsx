'use client'

import { cn } from '@/lib/utils'

/**
 * Lyra's mark: Vega, the lyre's bright star, held at the center of an orbit that carries
 * two smaller companions. Lyra is a constellation, so the mark is drawn as one rather than
 * as the generic sparkle cluster every assistant ships.
 *
 * The orbit is one ring with a single break in it, not a pair of arcs. A broken ring still
 * reads as a ring; two separate arcs read as two parentheses, and rotating them looks like
 * a mistake rather than an orbit. The companions ride on the ring, so when the mark turns
 * they travel with it and the motion has something to be about.
 *
 * Everything is `currentColor`, so a caller picks the tone by setting text color.
 */

const CENTER = 12
const ORBIT_RADIUS = 9.5

/** A concave four-point star: cusps pulled to `waist` so the arms read as light, not as a diamond. */
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

/** A point on the orbit, measured in degrees clockwise from twelve o'clock. */
function onOrbit(degrees: number): [number, number] {
  const radians = ((degrees - 90) * Math.PI) / 180
  return [CENTER + ORBIT_RADIUS * Math.cos(radians), CENTER + ORBIT_RADIUS * Math.sin(radians)]
}

const [COMPANION_X, COMPANION_Y] = onOrbit(48)
const [FAINT_X, FAINT_Y] = onOrbit(214)

const VEGA = starPath(CENTER, CENTER, 6.1, 1.35)
const COMPANION = starPath(COMPANION_X, COMPANION_Y, 2.2, 0.6)
const FAINT = starPath(FAINT_X, FAINT_Y, 1.4, 0.4)

// A ring broken at roughly one o'clock, so the outline has a beginning and the rotation is
// legible. Drawn as an arc pair on one path: from 20 degrees round to 4 degrees.
const [ORBIT_START_X, ORBIT_START_Y] = onOrbit(20)
const [ORBIT_END_X, ORBIT_END_Y] = onOrbit(4)
const ORBIT = [
  `M${ORBIT_START_X.toFixed(2)} ${ORBIT_START_Y.toFixed(2)}`,
  `A${ORBIT_RADIUS} ${ORBIT_RADIUS} 0 1 1 ${ORBIT_END_X.toFixed(2)} ${ORBIT_END_Y.toFixed(2)}`,
].join('')

type LyraMarkProps = {
  /** True while the model is working, which sets the orbit turning and the stars breathing. */
  thinking?: boolean
  className?: string
}

export function LyraMark({ thinking = false, className }: LyraMarkProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden
      className={cn('size-full', thinking && 'lyra-mark-thinking', className)}
    >
      <g className="lyra-mark-ring">
        <path
          d={ORBIT}
          stroke="currentColor"
          strokeWidth="1"
          strokeLinecap="round"
          opacity="0.32"
        />
        <path className="lyra-mark-companion" d={COMPANION} fill="currentColor" opacity="0.75" />
        <path className="lyra-mark-faint" d={FAINT} fill="currentColor" opacity="0.5" />
      </g>
      <path className="lyra-mark-star" d={VEGA} fill="currentColor" />
    </svg>
  )
}

/**
 * The mark on its accent disc, which is how it appears beside a message and on the empty
 * conversation. Decorative: an assistant message is already labelled by its position and
 * its actions.
 */
export function LyraAvatar({
  thinking = false,
  className,
}: {
  thinking?: boolean
  className?: string
}) {
  return (
    <span
      aria-hidden
      className={cn(
        'bg-accent-surface text-accent-surface-foreground flex shrink-0 items-center justify-center rounded-full',
        'size-7 p-[3px]',
        className,
      )}
    >
      <LyraMark thinking={thinking} />
    </span>
  )
}
