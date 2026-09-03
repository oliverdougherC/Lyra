'use client'

import { useState } from 'react'
import {
  AlertTriangle,
  Check,
  ChevronRight,
  Info,
  RefreshCw,
  TriangleAlert,
} from 'lucide-react'
import { toast } from 'sonner'

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { EndpointLocalityBadge } from '@/components/layout/endpoint-locality-badge'
import { Field, FieldDescription, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { Switch } from '@/components/ui/switch'
import { DesktopImportSection } from '@/components/settings/desktop-import-section'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import { api, ApiError } from '@/lib/api'
import { useClasses } from '@/lib/hooks/use-classes'
import {
  useClassWriterSettings,
  useSettings,
  useTestConnection,
  useTestExa,
  useTestTools,
  useTestVision,
  useUpdateSettings,
  useUpdateClassWriterSettings,
} from '@/lib/hooks/use-settings'
import { useTheme, type Theme } from '@/lib/theme'
import type { ConnectionTestResult, SettingsRead, SettingsUpdate } from '@/types'
import type { ClassRead } from '@/types'

const MIN_RECOMMENDED_CONTEXT = 8192

const THEME_CHOICES: [Theme, string, string][] = [
  ['light', 'Light', 'Use the parchment palette by default.'],
  ['system', 'System', 'Follow your operating system setting.'],
  ['dark', 'Dark', 'Always use the dark palette.'],
]

const THEME_SWATCHES: Record<Theme, string> = {
  light: 'border-border bg-background',
  system: 'border-border bg-accent-secondary',
  dark: 'border-border-strong bg-text-primary',
}

type TestState =
  | { status: 'idle' }
  | { status: 'testing' }
  | { status: 'done'; result: ConnectionTestResult }
  | { status: 'error'; message: string }

export function SettingsForm() {
  const { data: settings, isPending, isError, error, refetch } = useSettings()

  if (isPending) return <SettingsSkeleton />

  if (isError) {
    return (
      <Alert variant="destructive">
        <AlertTriangle />
        <AlertTitle>Could not load your settings</AlertTitle>
        <AlertDescription className="text-danger-text">
          <p>{error instanceof ApiError ? error.message : 'Could not read settings. Try again.'}</p>
          <Button variant="outline" size="sm" className="mt-2" onClick={() => void refetch()}>
            Retry
          </Button>
        </AlertDescription>
      </Alert>
    )
  }

  return <SettingsSections settings={settings} />
}

function SettingsSection({
  title,
  description,
  children,
}: {
  title: string
  description: string
  children: React.ReactNode
}) {
  return (
    // A section of the page, not a card on it: the hairline above does the separating,
    // and the fields stand on the same paper as everything else.
    <section className="border-border/70 border-t pt-6">
      <div className="mb-5">
        <h2 className="font-heading text-xl leading-tight font-medium tracking-tight">{title}</h2>
        <p className="text-text-secondary mt-1 text-sm">{description}</p>
      </div>
      {children}
    </section>
  )
}

function SettingsSections({ settings }: { settings: SettingsRead }) {
  const updateSettings = useUpdateSettings()
  const testConnection = useTestConnection()
  const testExa = useTestExa()
  const testTools = useTestTools()
  const testVision = useTestVision()
  const { theme, setTheme } = useTheme()

  const [endpoint, setEndpoint] = useState(settings.endpoint_url ?? '')
  const [apiKey, setApiKey] = useState('')
  const [exaApiKey, setExaApiKey] = useState('')
  const [contextWindow, setContextWindow] = useState(String(settings.context_window))
  const [parallelConcurrency, setParallelConcurrency] = useState(
    String(settings.parallel_concurrency),
  )
  const [models, setModels] = useState<string[]>([])
  const [test, setTest] = useState<TestState>({ status: 'idle' })

  // A save writes the canonical values back into the cache, and the inputs have to follow.
  // Adjusting during render rather than in an effect avoids a frame showing the stale text.
  const serverEcho = `${settings.endpoint_url ?? ''}|${settings.context_window}|${settings.parallel_concurrency}`
  const [lastEcho, setLastEcho] = useState(serverEcho)
  if (serverEcho !== lastEcho) {
    setLastEcho(serverEcho)
    setEndpoint(settings.endpoint_url ?? '')
    setContextWindow(String(settings.context_window))
    setParallelConcurrency(String(settings.parallel_concurrency))
  }

  // A successful test is what proves the endpoint answers, so the model select stays locked
  // until one lands rather than offering a list that cannot be populated.
  const modelsUnlocked = models.length > 0
  const hasEndpoint = endpoint.trim().length > 0

  async function save(patch: SettingsUpdate, success?: string): Promise<boolean> {
    try {
      await updateSettings.mutateAsync(patch)
      if (success) toast.success(success)
      return true
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : 'Could not save that change.')
      return false
    }
  }

  async function runTest() {
    setTest({ status: 'testing' })
    if (!(await save({ endpoint_url: endpoint.trim() || null }))) {
      setTest({ status: 'idle' })
      return
    }
    try {
      const result = await testConnection.mutateAsync()
      setTest({ status: 'done', result })
      setModels(result.ok && result.model_count > 0 ? (await api.listModels()).models : [])
    } catch (caught) {
      setModels([])
      setTest({
        status: 'error',
        message: caught instanceof ApiError ? caught.message : 'The connection test failed.',
      })
    }
  }

  const contextValue = Number(contextWindow)
  const contextTooSmall = Number.isFinite(contextValue) && contextValue < MIN_RECOMMENDED_CONTEXT

  return (
    <div className="space-y-8">
      <header className="pt-2 md:pt-6">
        <h1 className="font-display text-3xl leading-tight md:text-4xl">Settings</h1>
        <p className="text-text-secondary mt-1.5 text-sm">
          Lyra stores everything on this machine. These settings control the one part that can leave
          it.
        </p>
      </header>

      <SettingsSection
        title="Setup"
        description="Connect the model that answers questions about your course materials."
      >
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="endpoint-url">Endpoint URL</FieldLabel>
            <Input
              id="endpoint-url"
              inputMode="url"
              autoComplete="off"
              placeholder="http://127.0.0.1:8080/v1"
              value={endpoint}
              onChange={(event) => setEndpoint(event.target.value)}
              onBlur={() => {
                const next = endpoint.trim() || null
                if (next !== settings.endpoint_url) void save({ endpoint_url: next })
              }}
            />
            <FieldDescription>
              Lyra works best with a local model server. Remote endpoints send your documents over
              the network.
            </FieldDescription>
            <div className="mt-2">
              <EndpointLocalityBadge />
            </div>
          </Field>

          <Field>
            <FieldLabel htmlFor="api-key">API key</FieldLabel>
            <div className="flex gap-2">
              <Input
                id="api-key"
                type="password"
                autoComplete="off"
                placeholder={settings.api_key_set ? 'Set. Type to replace.' : 'Not set'}
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
              />
              <Button
                variant="outline"
                disabled={apiKey.length === 0 || updateSettings.isPending}
                onClick={async () => {
                  if (await save({ api_key: apiKey }, 'API key saved.')) setApiKey('')
                }}
              >
                Save key
              </Button>
              {settings.api_key_set ? (
                <Button
                  variant="ghost"
                  onClick={() => void save({ api_key: '' }, 'API key removed.')}
                >
                  Remove
                </Button>
              ) : null}
            </div>
            <FieldDescription>
              {settings.api_key_set ? 'Set. ' : 'Not set. '}
              {settings.api_key_storage === 'keychain'
                ? 'Stored in your operating system keychain and never sent back to this screen.'
                : 'No keychain was available, so it is kept in a file inside your data directory with owner-only permissions.'}
            </FieldDescription>
          </Field>

          <Field>
            <FieldLabel>Connection</FieldLabel>
            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant="outline"
                onClick={runTest}
                disabled={test.status === 'testing' || !hasEndpoint}
              >
                {test.status === 'testing' ? <Spinner /> : null}
                Test connection
              </Button>
              <TestOutcome state={test} />
            </div>
          </Field>


          <Field>
            <FieldLabel htmlFor="model">Model</FieldLabel>
            <div className="flex gap-2">
              <Select
                value={settings.model ?? undefined}
                disabled={!modelsUnlocked}
                onValueChange={(value) => void save({ model: value }, 'Model selected.')}
              >
                <SelectTrigger id="model" className="flex-1">
                  {/* Radix renders nothing when the stored value has no matching item,
                      which is the normal state before a connection test: the model is
                      saved, the list is not loaded yet. Naming it here keeps the control
                      from reading as empty when it is not. */}
                  <SelectValue placeholder="Test the connection to choose a model">
                    {settings.model ?? undefined}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {models.map((name) => (
                    <SelectItem key={name} value={name}>
                      {name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                variant="outline"
                size="icon"
                aria-label="Refresh model list"
                disabled={test.status === 'testing' || !hasEndpoint}
                onClick={runTest}
              >
                <RefreshCw />
              </Button>
            </div>
            <FieldDescription>
              Populated by a successful connection test, so the list is always what the endpoint
              really offers.
            </FieldDescription>
          </Field>

        </FieldGroup>
      </SettingsSection>

      <SettingsSection
        title="Research"
        description="Optional web research for the writer: what it may search, and how its key is stored."
      >
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="exa-api-key">Exa API key</FieldLabel>
            <div className="flex flex-wrap gap-2">
              <Input
                id="exa-api-key"
                className="min-w-64 flex-1"
                type="password"
                autoComplete="off"
                placeholder={settings.exa_api_key_set ? 'Set. Type to replace.' : 'Not set'}
                value={exaApiKey}
                onChange={(event) => setExaApiKey(event.target.value)}
              />
              <Button
                variant="outline"
                disabled={exaApiKey.length === 0 || updateSettings.isPending}
                onClick={async () => {
                  if (await save({ exa_api_key: exaApiKey }, 'Exa key saved.')) setExaApiKey('')
                }}
              >
                Save key
              </Button>
              {settings.exa_api_key_set ? (
                <Button
                  variant="ghost"
                  onClick={() => void save({ exa_api_key: '' }, 'Exa key removed.')}
                >
                  Remove
                </Button>
              ) : null}
            </div>
            <FieldDescription>
              {settings.exa_api_key_set ? 'Set. ' : 'Not set. '}
              {settings.exa_api_key_storage === 'keychain'
                ? 'Stored in your operating system keychain and never sent back to this screen.'
                : 'No keychain was available, so it is kept in a file inside your data directory with owner-only permissions.'}
            </FieldDescription>
          </Field>

          <Field>
            <FieldLabel>Exa connection</FieldLabel>
            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant="outline"
                onClick={() => testExa.mutate()}
                disabled={testExa.isPending || !settings.exa_api_key_set}
              >
                {testExa.isPending ? <Spinner /> : null}
                Test Exa
              </Button>
              <ExaTestOutcome
                hasKey={settings.exa_api_key_set}
                pending={testExa.isPending}
                result={testExa.data}
              />
            </div>
            <FieldDescription>
              Web research is optional and never probed on launch. When you test or use it, Exa
              receives only the public search query and any public URLs Lyra is asked to retrieve.
            </FieldDescription>
          </Field>

          <Field orientation="horizontal">
            <div className="min-w-0 flex-1">
              <FieldLabel htmlFor="allow-web-research">Allow web research</FieldLabel>
              <FieldDescription>
                Lets the writer search public pages and save the passages it actually relies on.
                Every fetched source remains visible in the draft&apos;s source ledger.
              </FieldDescription>
            </div>
            <Switch
              id="allow-web-research"
              checked={settings.allow_web_research}
              onCheckedChange={(checked) => void save({ allow_web_research: checked })}
            />
          </Field>


          <Alert>
            <Info />
            <AlertTitle>Exa receives public web requests only</AlertTitle>
            <AlertDescription>
              Lyra does not send your uploaded document text, private class facts, filesystem paths,
              credentials, or prior private conversation content to Exa. Missing or failed Exa
              configuration disables web research without making the rest of Lyra unhealthy.
            </AlertDescription>
          </Alert>
        </FieldGroup>
      </SettingsSection>

      <SettingsSection
        title="Privacy"
        description="See what stays on this machine and control what may leave it."
      >
        <PrivacySection settings={settings} onSave={save} />
      </SettingsSection>

      <SettingsSection
        title="Appearance"
        description="Choose the visual mode Lyra uses on this device."
      >
        <RadioGroup
          value={theme}
          onValueChange={(value) => setTheme(value as Theme)}
          className="gap-2"
        >
          {THEME_CHOICES.map(([value, label, description]) => (
            <Label
              key={value}
              htmlFor={`theme-${value}`}
              className={`flex min-h-11 cursor-pointer items-center gap-3 rounded-md border px-3 py-2 transition-colors ${
                theme === value
                  ? 'border-accent-primary bg-accent-surface/50'
                  : 'border-border bg-card hover:bg-muted'
              }`}
            >
              <RadioGroupItem value={value} id={`theme-${value}`} />
              <span
                aria-hidden
                className={`size-5 shrink-0 rounded-sm border ${THEME_SWATCHES[value]}`}
              />
              <span className="min-w-0">
                <span className="block text-sm font-medium">{label}</span>
                <span className="block text-sm text-muted-foreground">{description}</span>
              </span>
            </Label>
          ))}
        </RadioGroup>
      </SettingsSection>

      <SettingsSection
        title="Import existing Lyra data"
        description="Bring in a previous checkout through a staged, verified desktop import. The source stays untouched."
      >
        <DesktopImportSection />
      </SettingsSection>

      <AdvancedSection>
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="context-window">Context window</FieldLabel>
            <Input
              id="context-window"
              type="number"
              min={1024}
              step={1024}
              value={contextWindow}
              onChange={(event) => setContextWindow(event.target.value)}
              onBlur={() => {
                const parsed = Number(contextWindow)
                if (!Number.isFinite(parsed) || parsed < 1024) {
                  setContextWindow(String(settings.context_window))
                  return
                }
                if (parsed !== settings.context_window) void save({ context_window: parsed })
              }}
            />
            {contextTooSmall ? (
              <p className="text-danger-text flex items-start gap-1.5 text-sm">
                <TriangleAlert className="mt-0.5 size-4 shrink-0" />
                Below 8192 tokens, retrieval has room for roughly one chunk and answers lose the
                surrounding material.
              </p>
            ) : (
              <FieldDescription>
                Tokens the tutor model accepts. Lyra divides this between the system prompt,
                history, retrieved material, and the answer.
              </FieldDescription>
            )}
          </Field>

          <Field>
            <FieldLabel>Checking solutions</FieldLabel>
            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant="outline"
                onClick={() => testTools.mutate()}
                disabled={testTools.isPending || !hasEndpoint}
              >
                {testTools.isPending ? <Spinner /> : null}
                Test tool support
              </Button>
              <ToolSupportOutcome settings={settings} pending={testTools.isPending} />
            </div>
            <FieldDescription>
              Lyra checks each solution against a computer algebra system, which needs an endpoint
              that supports tool calls. Without one, solving still works and every solution is
              marked <span className="whitespace-nowrap">Not checked</span>.
            </FieldDescription>
          </Field>

          <Field>
            <FieldLabel>Reading scanned pages</FieldLabel>
            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant="outline"
                onClick={() => testVision.mutate()}
                disabled={testVision.isPending || !hasEndpoint}
              >
                {testVision.isPending ? <Spinner /> : null}
                Test image support
              </Button>
              <VisionSupportOutcome settings={settings} pending={testVision.isPending} />
            </div>
            <FieldDescription>
              A scanned page has no text to extract, so Lyra reads it by sending a picture of the
              page to this endpoint. Without a model that can see images, scanned documents stay
              unreadable and Lyra says so on the document rather than offering to read them.
            </FieldDescription>
          </Field>

          <Field orientation="horizontal">
            <div className="min-w-0 flex-1">
              <FieldLabel htmlFor="parallel-requests">Parallel writer requests</FieldLabel>
              <FieldDescription>
                Runs independent research, drafting, and review jobs together when the endpoint can
                safely accept them. Leave this off for a serial local server.
              </FieldDescription>
            </div>
            <Switch
              id="parallel-requests"
              checked={settings.parallel_requests}
              onCheckedChange={(checked) => void save({ parallel_requests: checked })}
            />
          </Field>

          <Field>
            <FieldLabel htmlFor="parallel-concurrency">Maximum concurrent requests</FieldLabel>
            <Input
              id="parallel-concurrency"
              className="w-28"
              type="number"
              min={1}
              max={16}
              step={1}
              disabled={!settings.parallel_requests}
              value={parallelConcurrency}
              onChange={(event) => setParallelConcurrency(event.target.value)}
              onBlur={() => {
                const parsed = Number(parallelConcurrency)
                if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > 16) {
                  setParallelConcurrency(String(settings.parallel_concurrency))
                  return
                }
                if (parsed !== settings.parallel_concurrency) {
                  void save({ parallel_concurrency: parsed })
                }
              }}
            />
            <FieldDescription>
              A bound, not a target. Lyra only fans out stages that do not depend on one another.
            </FieldDescription>
          </Field>

          <ClassResearchOverrides />
        </FieldGroup>
      </AdvancedSection>
    </div>
  )
}

/**
 * The most technical surface on the page: context size, endpoint capability checks, parallel
 * writer tuning, and per-course research. Real configuration, but for the minority of setups
 * that need it - so it stays collapsed by default and never competes with the sections a
 * student actually uses.
 */
function AdvancedSection({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false)
  return (
    <section className="border-border/70 border-t pt-6">
      <Collapsible open={open} onOpenChange={setOpen}>
        <CollapsibleTrigger
          className="focus-visible:ring-ring [&[data-state=open]>svg]:rotate-90 flex w-full items-center justify-between gap-3 rounded-md text-left focus-visible:ring-2 focus-visible:outline-none"
        >
          <div className="min-w-0">
            <h2 className="font-heading text-xl leading-tight font-medium tracking-tight">
              Advanced
            </h2>
            <p className="text-text-secondary mt-1 text-sm">
              Runtime tuning and diagnostics for unusual setups: context size, endpoint capability
              checks, parallel writer requests, and per-course research.
            </p>
          </div>
          <ChevronRight
            aria-hidden
            className="size-5 shrink-0 text-text-tertiary transition-transform"
          />
        </CollapsibleTrigger>
        <CollapsibleContent>
          <div className="mt-5">{children}</div>
        </CollapsibleContent>
      </Collapsible>
    </section>
  )
}

function ClassResearchOverrides() {
  const classes = useClasses()

  if (!classes.data?.length) return null

  return (
    <Field>
      <FieldLabel>Course web research</FieldLabel>
      <FieldDescription>
        Each course inherits the global choice unless you explicitly allow or block it here.
      </FieldDescription>
      <div className="mt-2 grid gap-2">
        {classes.data.map((course) => (
          <ClassResearchOverride key={course.id} course={course} />
        ))}
      </div>
    </Field>
  )
}

function ClassResearchOverride({ course }: { course: ClassRead }) {
  const writerSettings = useClassWriterSettings(course.id)
  const update = useUpdateClassWriterSettings()
  const override = writerSettings.data?.overrides.allow_web_research
  const value =
    override === null || override === undefined ? 'inherit' : override ? 'allow' : 'block'

  async function change(next: string) {
    const allowWebResearch = next === 'inherit' ? null : next === 'allow'
    try {
      await update.mutateAsync({
        classId: course.id,
        body: { allow_web_research: allowWebResearch },
      })
    } catch (caught) {
      toast.error(
        caught instanceof ApiError ? caught.message : 'Could not save that course override.',
      )
    }
  }

  return (
    <div className="flex items-center justify-between gap-3 rounded-md border border-border/70 px-3 py-2">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium">{course.name}</p>
        {writerSettings.data ? (
          <p className="text-text-secondary text-xs">
            Effective: {writerSettings.data.effective.allow_web_research ? 'allowed' : 'blocked'}
          </p>
        ) : null}
      </div>
      <Select
        value={value}
        disabled={writerSettings.isPending || update.isPending}
        onValueChange={(next) => void change(next)}
      >
        <SelectTrigger size="sm" className="w-28" aria-label={`${course.name} web research`}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="inherit">Inherit</SelectItem>
          <SelectItem value="allow">Allow</SelectItem>
          <SelectItem value="block">Block</SelectItem>
        </SelectContent>
      </Select>
    </div>
  )
}

type PrivacySectionProps = {
  settings: SettingsRead
  onSave: (patch: SettingsUpdate, success?: string) => Promise<boolean>
}

function PrivacySection({ settings, onSave }: PrivacySectionProps) {
  const isRemote = settings.endpoint_is_local === false
  const locality =
    settings.endpoint_is_local === null
      ? { label: 'No endpoint configured', className: 'text-text-tertiary' }
      : settings.endpoint_is_local
        ? { label: `Local, ${settings.endpoint_host}`, className: 'text-success-text' }
        : {
            label: `Remote, ${settings.endpoint_host}`,
            className: settings.remote_ack ? 'text-info-text' : 'text-danger-text',
          }

  return (
    <div className="space-y-4">
      <div className="space-y-3 text-sm">
        <div>
          <p className="font-medium">Stays on this machine</p>
          <p className="text-muted-foreground">
            Reading your files, splitting them, computing embeddings, and all storage. The embedding
            model runs locally
            {settings.embedding_model ? ` as ${settings.embedding_model}` : ''}.
          </p>
        </div>
        <div>
          <p className="font-medium">Leaves this machine</p>
          <p className="text-muted-foreground">
            Tutor requests go only to the endpoint above: your question, the retrieved passages, and
            the conversation so far. Web research is separate: if you enable it, Exa receives only
            public search queries and requested public URLs.
          </p>
        </div>
        <div>
          <p className="font-medium">Current endpoint</p>
          <p className={locality.className}>{locality.label}</p>
        </div>
      </div>

      {isRemote ? (
        <Alert variant="destructive">
          <Info />
          <AlertTitle>This endpoint is not on your machine</AlertTitle>
          <AlertDescription>
            <div className="mt-2 flex items-start gap-3">
              <Switch
                id="remote-ack"
                checked={settings.remote_ack}
                onCheckedChange={(checked) => void onSave({ remote_ack: checked })}
              />
              <Label htmlFor="remote-ack" className="leading-5 font-normal">
                I understand my document text will be sent to this endpoint.
              </Label>
            </div>
            {settings.remote_ack ? null : (
              <p className="mt-2">
                Until this is on, Lyra will not send whole documents out for profile extraction.
              </p>
            )}
          </AlertDescription>
        </Alert>
      ) : null}

      <div className="flex items-start justify-between gap-4">
        <div>
          <Label htmlFor="extraction-enabled">Automatic profile extraction</Label>
          <p className="text-muted-foreground text-sm">
            After each upload, Lyra reads the whole document once to pull out dates, topics, and
            grading. It costs one full-document pass per upload.
          </p>
        </div>
        <Switch
          id="extraction-enabled"
          checked={settings.extraction_enabled}
          onCheckedChange={(checked) => void onSave({ extraction_enabled: checked })}
        />
      </div>
    </div>
  )
}

function TestOutcome({ state }: { state: TestState }) {
  if (state.status === 'idle') return null

  if (state.status === 'testing') {
    return <span className="text-muted-foreground text-sm">Testing the endpoint...</span>
  }

  if (state.status === 'error') {
    return <span className="text-danger-text text-sm">{state.message}</span>
  }

  const { result } = state
  if (result.ok && result.model_count > 0) {
    return (
      <span className="text-success-text flex items-center gap-1.5 text-sm">
        <Check className="size-4" />
        {result.message}
      </span>
    )
  }
  if (result.ok) {
    return (
      <span className="text-info-text flex items-center gap-1.5 text-sm">
        <Info className="size-4" />
        {result.message}
      </span>
    )
  }
  return <span className="text-danger-text text-sm">{result.message}</span>
}

function ExaTestOutcome({
  hasKey,
  pending,
  result,
}: {
  hasKey: boolean
  pending: boolean
  result: { ok: boolean; status: string; message: string } | undefined
}) {
  if (pending) return <span className="text-muted-foreground text-sm">Testing Exa...</span>
  if (!hasKey) {
    return <span className="text-muted-foreground text-sm">Add a key to test Exa.</span>
  }
  if (!result) {
    return <span className="text-muted-foreground text-sm">Not tested yet.</span>
  }
  if (result.ok) {
    return (
      <span className="text-success-text flex items-center gap-1.5 text-sm">
        <Check className="size-4" />
        {result.message}
      </span>
    )
  }
  return (
    <span
      className={
        result.status === 'temporarily_unavailable'
          ? 'text-info-text text-sm'
          : 'text-danger-text text-sm'
      }
    >
      {result.message}
    </span>
  )
}

function SettingsSkeleton() {
  return (
    <div className="space-y-8" aria-busy="true" aria-label="Loading settings">
      <div className="space-y-2">
        <Skeleton className="h-8 w-40" />
        <Skeleton className="h-4 w-80" />
      </div>
      {[0, 1, 2].map((section) => (
        <div key={section} className="space-y-4">
          <Skeleton className="h-6 w-32" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ))}
    </div>
  )
}

/**
 * Three states, not two. Null means nobody has asked this endpoint yet, and saying so is
 * different from saying no: one costs a click, the other costs every verdict in the app.
 */
function ToolSupportOutcome({ settings, pending }: { settings: SettingsRead; pending: boolean }) {
  if (pending) return <span className="text-muted-foreground text-sm">Asking the endpoint...</span>

  if (settings.tools_supported === null) {
    return (
      <span className="text-muted-foreground text-sm">
        Not checked yet. Lyra will ask the first time you solve a problem set.
      </span>
    )
  }

  if (settings.tools_supported) {
    return (
      <span className="text-success-text flex items-center gap-1.5 text-sm">
        <Check className="size-4" />
        {settings.tools_message ?? 'This endpoint can run the checks Lyra verifies with.'}
      </span>
    )
  }

  return (
    <span className="text-info-text text-sm">
      {settings.tools_message ?? 'This endpoint cannot run tool calls. Solving still works.'}
    </span>
  )
}

/**
 * The same three states as tool support, and the same reason for three rather than two.
 * What differs is the cost of a no: a scanned document simply cannot be read, so the
 * document row withholds the offer instead of letting it fail one page at a time.
 */
function VisionSupportOutcome({ settings, pending }: { settings: SettingsRead; pending: boolean }) {
  if (pending) return <span className="text-muted-foreground text-sm">Asking the endpoint...</span>

  if (settings.vision_supported === null) {
    return (
      <span className="text-muted-foreground text-sm">
        Not checked yet. Lyra will offer to read scans and report if it cannot.
      </span>
    )
  }

  if (settings.vision_supported) {
    return (
      <span className="text-success-text flex items-center gap-1.5 text-sm">
        <Check className="size-4" />
        {settings.vision_message ?? 'This endpoint can read images.'}
      </span>
    )
  }

  return (
    <span className="text-info-text text-sm">
      {settings.vision_message ?? 'This endpoint cannot read images, so scans stay unreadable.'}
    </span>
  )
}
