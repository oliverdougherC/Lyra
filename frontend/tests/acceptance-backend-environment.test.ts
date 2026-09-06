import { expect, it } from 'vitest'
import { isolatedBackendEnvironment } from '../e2e/acceptance/backend-environment'

it('overrides inherited OS keyring selection while retaining the isolated profile', () => {
  expect(
    isolatedBackendEnvironment({
      LYRA_DATA_DIR: '/tmp/lyra-test',
      PYTHON_KEYRING_BACKEND: 'keyring.backends.macOS.Keyring',
      MISSING: undefined,
    }),
  ).toEqual({
    LYRA_DATA_DIR: '/tmp/lyra-test',
    PYTHON_KEYRING_BACKEND: 'keyring.backends.fail.Keyring',
  })
})
