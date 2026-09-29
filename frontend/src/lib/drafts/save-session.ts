import type { SaveEngine, SaveStateName } from './save-engine'

type StateListener = (state: SaveStateName, detail?: string) => void
interface SaveSession {
  key: string
  engine: SaveEngine
  listeners: Map<symbol, StateListener>
}

// A pending document keeps one writer across mounts. View listeners are leases;
// late cleanup can release only its own view and its own quiescent session.
const sessions = new Map<string, SaveSession>()
const owners = new Map<string, symbol>()

export function saveOwnerFor(key: string): symbol {
  let owner = owners.get(key)
  if (!owner) {
    owner = Symbol('draft-save')
    owners.set(key, owner)
  }
  return owner
}

export interface SaveSessionHandle {
  engine: SaveEngine
  attach: (listener: StateListener) => void
  detach: () => void
}

export function saveSessionFor(
  key: string,
  createEngine: (notify: StateListener) => SaveEngine,
): SaveSessionHandle {
  let session = sessions.get(key)
  if (!session) {
    const listeners = new Map<symbol, StateListener>()
    const engine = createEngine((state, detail) => {
      for (const listener of listeners.values()) listener(state, detail)
    })
    session = { key, engine, listeners }
    sessions.set(key, session)
  }
  return handle(session)
}

export function saveSessionEngine(key: string): SaveEngine | null {
  return sessions.get(key)?.engine ?? null
}

function handle(session: SaveSession): SaveSessionHandle {
  const lease = Symbol('draft-view')
  let detached = false
  return {
    engine: session.engine,
    attach(listener): void {
      detached = false
      session.listeners.set(lease, listener)
      const snapshot = session.engine.snapshot()
      listener(snapshot.state, snapshot.detail ?? undefined)
      session.engine.resume()
    },
    detach(): void {
      if (detached) return
      detached = true
      session.listeners.delete(lease)
      // Another mounted view still owns presentation and the newest desired body.
      if (session.listeners.size > 0) return
      // The engine owns the newest desired text. An uninitialized or stale view
      // must not replace it with its local buffer during teardown.
      const finalFlush = session.engine.flush(null)
      session.engine.suspend()
      void finalFlush.then((result) => {
        if (!result.ok || sessions.get(session.key) !== session || session.listeners.size > 0)
          return
        const engine = session.engine
        if (engine.saving() || engine.conflict() !== null || engine.pendingContent() !== null)
          return
        sessions.delete(session.key)
        owners.delete(session.key)
        engine.dispose()
      })
    },
  }
}
