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

export function ClassProfileSheet({
  classId,
  open,
  onOpenChange,
}: {
  classId: number
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="flex w-full flex-col sm:max-w-md">
        <SheetHeader className="shrink-0 border-b px-5 py-4">
          <SheetTitle>Class profile</SheetTitle>
          <SheetDescription>
            What Lyra has worked out about this class from everything you uploaded.
          </SheetDescription>
        </SheetHeader>
        {/* Sized by the sheet rather than by a viewport calculation, which drifts the
            moment the header's copy wraps to a second line. */}
        <ScrollArea className="min-h-0 flex-1 px-5 py-5">
          {/* Loaded only while the sheet is open: the header renders this on every class
              route, and a closed sheet asking for the profile would fetch it everywhere. */}
          <ProfileFacts classId={classId} enabled={open} />
        </ScrollArea>
      </SheetContent>
    </Sheet>
  )
}
