/** Acceptance profiles must never read or mutate the operator's OS credential store. */
export function isolatedBackendEnvironment(env: NodeJS.ProcessEnv): Record<string, string> {
  return {
    ...Object.fromEntries(
      Object.entries(env).filter((entry): entry is [string, string] => entry[1] !== undefined),
    ),
    // Unlike NullKeyring, this raises on writes so real Lyra storage uses its private fallback.
    PYTHON_KEYRING_BACKEND: 'keyring.backends.fail.Keyring',
  }
}
