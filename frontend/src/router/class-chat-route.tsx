import { lazy, useState, type ComponentType } from 'react'

let loadedChat: ComponentType | null = null

export async function preloadClassChat() {
  const module = await import('@/app/classes/[id]/chat/page')
  loadedChat = module.default
  return module
}

const LazyChat = lazy(preloadClassChat)

/** Preloaded handoffs mount synchronously; direct links keep normal route splitting. */
export function ClassChatRoute() {
  const [Page] = useState(() => loadedChat ?? LazyChat)
  return <Page />
}
