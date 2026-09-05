import { expect, it } from 'vitest'
import { ownedFixturePids, survivingOwnedFixtures } from '../e2e/acceptance/process-ownership'

it('excludes fixtures belonging to another checkout or another run in this checkout', () => {
  const listing = `
100 1 100 uv run python -m uvicorn acceptance.backend_harness:app
101 100 100 python -m uvicorn acceptance.backend_harness:app
102 101 102 python /repo/frontend/e2e/acceptance/fake-helper.py
200 1 200 uv run python -m uvicorn acceptance.backend_harness:app
201 200 200 python -m uvicorn acceptance.backend_harness:app
202 201 202 python /other/frontend/e2e/acceptance/fake-helper.py
300 1 300 python /repo/frontend/e2e/acceptance/fake-helper.py
400 1 400 python -m backend.main
`
  expect(ownedFixturePids(listing, [100])).toEqual([100, 101, 102])
  expect(ownedFixturePids(listing, [])).toEqual([])
})

it('recognizes an orphan in the owned group without matching a neighboring group', () => {
  expect(
    ownedFixturePids(
      '101 1 100 python -m uvicorn acceptance.backend_harness:app\n201 1 200 python -m uvicorn acceptance.backend_harness:app',
      [100],
    ),
  ).toEqual([101])
})

it('retains a detached child after its parent exits but excludes a recycled PID', () => {
  const captured = new Map([
    [102, 'original child birth'],
    [101, 'original backend birth'],
  ])
  const now = new Map([
    [102, 'original child birth'],
    [101, 'different process birth'],
    [202, 'foreign birth'],
  ])
  expect(survivingOwnedFixtures(captured, (pid) => now.get(pid) ?? null)).toEqual([102])
  now.delete(102)
  expect(survivingOwnedFixtures(captured, (pid) => now.get(pid) ?? null)).toEqual([])
})
