// A dirty engine stays represented even after its editor unmounts while a write
// is pending. Only a confirmed save or explicit discard removes the blocker.
const unconfirmed = new Set<symbol>()
export function reportUpdateSaveState(owner: symbol, saved: boolean): void {
  if (saved) unconfirmed.delete(owner)
  else unconfirmed.add(owner)
}
export function assertUpdateSafe(action = 'installing an update'): void {
  if (unconfirmed.size) {
    throw new Error(`Finish saving or resolve the writing conflict before ${action}.`)
  }
  const event = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(event)
  if (event.defaultPrevented) throw new Error(`Save your changes before ${action}.`)
}
