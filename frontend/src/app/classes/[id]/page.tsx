'use client'

import { useParams, useSearchParams } from 'next/navigation'

import { ClassHub, readHubTab } from '@/components/classes/class-hub'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { useClass } from '@/lib/hooks/use-classes'

function readClassId(value: string | string[] | undefined): number | null {
  const raw = Array.isArray(value) ? value[0] : value
  const classId = Number(raw)
  return Number.isSafeInteger(classId) && classId > 0 ? classId : null
}

/**
 * A class opens onto itself.
 *
 * Clicking a class used to start a conversation, which is one of the four things a class
 * holds and the only one that got a front door. This page is that door: the chats, the
 * solution sets, the documents, and the profile, each with the actions that belong to it.
 * The conversation moved one level down, to `/classes/[id]/chat`.
 */
export default function ClassHubPage() {
  const params = useParams<{ id: string }>()
  const classId = readClassId(params.id)
  // Read directly rather than under a Suspense boundary, as the chat route alongside this
  // one does. A boundary here rendered the whole hub twice - once as the fallback, once
  // for real - and left both copies in the document, so the upload inputs and the pane's
  // scroll container each existed under two elements carrying the same id.
  const tab = useSearchParams().get('tab')
  const classQuery = useClass(classId ?? Number.NaN)

  if (classId === null) {
    return (
      <Alert variant="destructive" className="max-w-xl">
        <AlertTitle>That class could not be opened</AlertTitle>
        <AlertDescription>Return to Classes and choose a class from the list.</AlertDescription>
      </Alert>
    )
  }

  if (classQuery.isError) {
    return (
      <Alert variant="destructive" className="max-w-xl">
        <AlertTitle>Could not load this class</AlertTitle>
        <AlertDescription>
          <p>
            {classQuery.error instanceof Error ? classQuery.error.message : 'Try loading it again.'}
          </p>
          <Button
            variant="outline"
            size="sm"
            className="mt-3"
            onClick={() => void classQuery.refetch()}
          >
            Retry
          </Button>
        </AlertDescription>
      </Alert>
    )
  }

  return <ClassHub classId={classId} tab={readHubTab(tab)} />
}
