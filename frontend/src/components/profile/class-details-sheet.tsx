'use client'

import { ProfileFacts } from '@/components/profile/profile-facts'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import type { ClassRead } from '@/types'

/**
 * The class itself, in one sheet: the facts the class holds (name, code, semester) beside
 * what Lyra has worked out from everything uploaded about it (the profile facts).
 *
 * The header used to hold a Profile button that opened the facts sheet on every class
 * route. That moved here, into the hub's own menu, where the class - not a control - is
 * the subject. The sheet loads only while open, and it is where a fact the class holds
 * can be corrected in place.
 */
export function ClassDetailsSheet({
  classId,
  open,
  onOpenChange,
  klass,
}: {
  classId: number
  open: boolean
  onOpenChange: (open: boolean) => void
  klass?: ClassRead | null
}) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="flex w-full flex-col sm:max-w-md">
        <SheetHeader className="shrink-0 border-b px-5 py-4">
          <SheetTitle>Class details</SheetTitle>
          <SheetDescription>
            What this class is, and what Lyra has worked out from everything you uploaded.
          </SheetDescription>
        </SheetHeader>
        <ScrollArea className="min-h-0 flex-1 px-5 py-5">
          {klass ? (
            <div className="flex flex-col gap-1.5">
              <InfoRow label="Name" value={klass.name} />
              {klass.code ? <InfoRow label="Code" value={klass.code} /> : null}
              {klass.semester ? <InfoRow label="Semester" value={klass.semester} /> : null}
            </div>
          ) : null}
          <div className="border-border mb-4 mt-4 border-t" />
          {/* Loaded only while the sheet is open: a closed sheet asking for the profile
              would fetch on every class route. */}
          <ProfileFacts classId={classId} enabled={open} />
        </ScrollArea>
      </SheetContent>
    </Sheet>
  )
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-sm">
      <span className="text-text-tertiary shrink-0">{label}</span>
      <span className="min-w-0 truncate font-medium text-text-primary">{value}</span>
    </div>
  )
}
