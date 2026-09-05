import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { CommandConfirmationCard, type AgentCommandRequest } from '@/components/agent'

const COMMAND: AgentCommandRequest = {
  id: 7,
  argv: ['pytest', '-q', 'tests/test_lab.py'],
  cwd: 'lab-repo',
  reason: 'Verify the proposed fix against the lab tests.',
  expectedSignal: 'A passing regression test run.',
  timeoutSeconds: 120,
  networkRisk: 'possible',
  state: 'pending',
}

describe('CommandConfirmationCard', () => {
  it('shows the readable command and consequences with exact arguments available in Details', async () => {
    render(<CommandConfirmationCard command={COMMAND} />)

    expect(screen.getByText('Runs exactly as shown')).toBeInTheDocument()
    expect(screen.getByText('Network effects are possible')).toBeInTheDocument()
    expect(screen.getByText('pytest -q tests/test_lab.py')).toBeInTheDocument()
    await userEvent.click(screen.getByText('Argument details'))
    expect(screen.getByRole('list', { name: 'Command arguments' })).toHaveTextContent('0:pytest')
    expect(screen.getByText('lab-repo')).toBeInTheDocument()
    expect(screen.getByText('120 seconds')).toBeInTheDocument()
  })

  it('emits explicit confirm and reject actions', async () => {
    const onConfirm = vi.fn()
    const onReject = vi.fn()
    render(<CommandConfirmationCard command={COMMAND} onConfirm={onConfirm} onReject={onReject} />)

    await userEvent.click(screen.getByRole('button', { name: 'Confirm and run' }))
    await userEvent.click(screen.getByRole('button', { name: 'Reject command' }))

    expect(onConfirm).toHaveBeenCalledTimes(1)
    expect(onReject).toHaveBeenCalledTimes(1)
  })

  it('renders persisted output after a completed run', () => {
    render(
      <CommandConfirmationCard
        command={{
          ...COMMAND,
          state: 'completed',
          confirmedAtLabel: 'just now',
          exitCode: 0,
          stdout: '2 passed',
          stderr: '',
          truncated: true,
        }}
      />,
    )

    expect(screen.getByText('Completed')).toBeInTheDocument()
    expect(screen.getByText('Exit 0')).toBeInTheDocument()
    expect(screen.getByText('Output truncated')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Output' })).toHaveTextContent('2 passed')
    expect(screen.queryByRole('button', { name: 'Confirm and run' })).not.toBeInTheDocument()
  })
})
