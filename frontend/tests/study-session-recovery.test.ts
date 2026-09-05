import { beforeEach, describe, expect, it } from 'vitest'

import {
  ownSessionRecovery,
  readSessionRecovery,
  readSessionRecoveryRecord,
  type SessionRecovery,
} from '@/lib/study-session-recovery'

const key = 'lyra:study-session:v1:8'
const empty: SessionRecovery = {
  queue: [],
  total: 0,
  ratings: { again: 0, hard: 0, good: 0, easy: 0 },
  states: [],
  operation: null,
}

beforeEach(() => sessionStorage.clear())

describe('durable recovery invariants', () => {
  it.each([
    { ...empty, operation: { id: 'possibly-committed', rating: 'good' } },
    { ...empty, total: 1 },
    { ...empty, ratings: { ...empty.ratings, good: -1 } },
    { ...empty, ratings: { ...empty.ratings, easy: 0.5 } },
  ])('rejects inconsistent snapshots without modifying evidence', (snapshot) => {
    const raw = JSON.stringify(snapshot)
    sessionStorage.setItem(key, raw)
    expect(() => readSessionRecovery(8)).toThrow()
    expect(sessionStorage.getItem(key)).toBe(raw)
  })

  it('revokes old owners and refuses writes after unmount', () => {
    const old = ownSessionRecovery(8, sessionStorage.getItem(key))
    old.save(empty)
    const current = ownSessionRecovery(8, sessionStorage.getItem(key))
    current.save({
      ...empty,
      unresolved: [{ partId: 11, operation: { id: 'retained', rating: 'good' } }],
    })
    const raw = sessionStorage.getItem(key)
    expect(old.current()).toBe(false)
    expect(() => old.save(empty)).toThrow()
    old.release()
    expect(current.current()).toBe(true)
    current.release()
    expect(() => current.save(empty)).toThrow()
    expect(sessionStorage.getItem(key)).toBe(raw)
  })

  it('refuses external revision changes even while mounted', () => {
    const owner = ownSessionRecovery(8, sessionStorage.getItem(key))
    owner.save(empty)
    const external = JSON.stringify({
      ...empty,
      unresolved: [{ partId: 12, operation: { id: 'other', rating: 'easy' } }],
    })
    sessionStorage.setItem(key, external)
    expect(() => owner.save(empty)).toThrow('Saved recovery changed')
    expect(sessionStorage.getItem(key)).toBe(external)
    owner.release()
  })
})

it('refuses ownership when the snapshot changes between render read and effect claim', () => {
  sessionStorage.setItem(key, JSON.stringify(empty))
  const rendered = readSessionRecoveryRecord(8)
  const old = ownSessionRecovery(8, rendered.revision)
  old.save({ ...empty, unresolved: [{ partId: 12, operation: { id: 'newer', rating: 'hard' } }] })
  const newer = sessionStorage.getItem(key)
  expect(() => ownSessionRecovery(8, rendered.revision)).toThrow('Saved recovery changed')
  expect(sessionStorage.getItem(key)).toBe(newer)
  expect(old.current()).toBe(true)
  old.release()
})
