/**
 * Fake OpenAI-compatible tutor endpoint for acceptance tests.
 *
 * Speaks the chat-completions protocol (streaming and non-streaming) on a fixed
 * port.  Tests configure its behaviour through a small control API so that the
 * same server can produce success, timeout, disconnect, malformed response, and
 * error-before-stream outcomes deterministically.
 *
 * The responses are intentionally simple: streaming produces one sentence of
 * text, non-streaming returns either plain text or schema-matched JSON when
 * the request includes `response_format`.
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

type Mode =
  | 'success'
  | 'timeout'
  | 'disconnect-before'
  | 'disconnect-mid'
  | 'malformed'
  | 'error-before-stream'
  | 'error-mid-stream'
  | 'context-window-error'
  | 'auth-error'
  | 'consent-refusal'
  | 'empty-response'

interface QueuedResponse {
  content: string
  stream?: boolean
}

interface RecordedRequest {
  timestamp: number
  url: string
  method: string
  body: unknown
}

/* ------------------------------------------------------------------ */
/*  Canned study responses                                             */
/* ------------------------------------------------------------------ */

const CANNED_TOPICS = JSON.stringify({
  topics: ['Thermodynamics Fundamentals', 'Laws of Thermodynamics'],
})

function cannedFlashcards(count: number, topic: string): string {
  const cards = Array.from({ length: count }, (_, i) => ({
    front: `[${topic}] What is the ${i === 0 ? 'first' : i === 1 ? 'second' : 'third'} law of thermodynamics?`,
    back: `The ${i === 0 ? 'first' : i === 1 ? 'second' : 'third'} law states that ${i === 0 ? 'energy cannot be created or destroyed' : i === 1 ? 'entropy always increases in an isolated system' : 'entropy approaches a minimum at absolute zero'}.`,
    topic,
  }))
  return JSON.stringify({ cards })
}

function cannedQuiz(count: number, difficulty: string): string {
  const questions = Array.from({ length: count }, (_, i) => ({
    type: 'mcq' as const,
    question: `Question ${i + 1}: What does the ${i === 0 ? 'first' : i === 1 ? 'second' : 'third'} law of thermodynamics state?`,
    options: [
      `Energy is conserved${i > 0 ? ` (option ${i})` : ''}`,
      'Entropy increases',
      'Temperature is absolute',
      'Work equals heat',
    ],
    correct_index: 0,
    explanation: `The correct answer relates to thermodynamic principle ${i + 1}.`,
    topic: 'Thermodynamics Fundamentals',
    difficulty,
  }))
  return JSON.stringify({ questions })
}

/* ------------------------------------------------------------------ */
/*  Server                                                             */
/* ------------------------------------------------------------------ */

export class TutorFixture {
  private server: Server
  private port: number
  private mode: Mode = 'success'
  private queue: QueuedResponse[] = []
  private requests: RecordedRequest[] = []

  constructor(port: number) {
    this.port = port
    this.server = createServer((req, res) => void this.handle(req, res))
  }

  async start(): Promise<void> {
    return new Promise((resolve, reject) => {
      this.server.listen(this.port, '127.0.0.1', () => resolve())
      this.server.once('error', reject)
    })
  }

  async stop(): Promise<void> {
    return new Promise((resolve) => {
      this.server.close(() => resolve())
    })
  }

  get baseUrl(): string {
    return `http://127.0.0.1:${this.port}/v1`
  }

  /* ---- private --------------------------------------------------- */

  private async handle(req: IncomingMessage, res: ServerResponse) {
    const body = await readBody(req)

    // Control API — not behind the /v1 prefix
    if (req.url?.startsWith('/_control/')) {
      this.handleControl(req, res, body)
      return
    }

    this.requests.push({
      timestamp: Date.now(),
      url: req.url ?? '',
      method: req.method ?? '',
      body: body ? JSON.parse(body) : null,
    })

    if (req.url === '/v1/models' && req.method === 'GET') {
      json(res, 200, {
        object: 'list',
        data: [
          {
            id: 'test-model',
            object: 'model',
            owned_by: 'acceptance-fixture',
          },
        ],
      })
      return
    }

    if (req.url === '/v1/chat/completions' && req.method === 'POST') {
      const parsed = body ? JSON.parse(body) : {}
      await this.handleChatCompletion(parsed, res)
      return
    }

    res.writeHead(404)
    res.end('Not Found')
  }

  private handleControl(req: IncomingMessage, res: ServerResponse, body: string) {
    const path = req.url!.replace('/_control/', '')

    if (path === 'mode' && req.method === 'POST') {
      const { mode } = JSON.parse(body)
      this.mode = mode as Mode
      json(res, 200, { ok: true, mode: this.mode })
      return
    }

    if (path === 'enqueue' && req.method === 'POST') {
      const item = JSON.parse(body) as QueuedResponse
      this.queue.push(item)
      json(res, 200, { ok: true, queued: this.queue.length })
      return
    }

    if (path === 'requests' && req.method === 'GET') {
      json(res, 200, this.requests)
      return
    }

    if (path === 'clear' && req.method === 'POST') {
      this.queue = []
      this.requests = []
      this.mode = 'success'
      json(res, 200, { ok: true })
      return
    }

    res.writeHead(404)
    res.end()
  }

  private async handleChatCompletion(body: Record<string, unknown>, res: ServerResponse) {
    const wantStream = body.stream === true

    // Check for enqueued responses first
    if (this.queue.length > 0) {
      const queued = this.queue.shift()!
      if (wantStream) {
        this.streamResponse(res, queued.content)
      } else {
        this.jsonCompletion(res, queued.content)
      }
      return
    }

    // Dispatch on current mode
    switch (this.mode) {
      case 'success':
        break // fall through to content generation
      case 'timeout':
        // Hold the connection open until the client gives up
        await new Promise((r) => setTimeout(r, 120_000))
        return
      case 'disconnect-before':
        res.destroy()
        return
      case 'disconnect-mid':
        if (wantStream) {
          this.streamPartialThenDisconnect(res)
        } else {
          res.destroy()
        }
        return
      case 'malformed':
        res.writeHead(200, { 'content-type': 'application/json' })
        res.end('NOT-JSON{{{')
        return
      case 'error-before-stream':
        json(res, 500, {
          error: { message: 'Injected pre-stream error', type: 'server_error' },
        })
        return
      case 'error-mid-stream':
        if (wantStream) {
          this.streamWithMidError(res)
        } else {
          json(res, 500, { error: { message: 'Injected error' } })
        }
        return
      case 'context-window-error':
        json(res, 400, {
          error: {
            message:
              "This model's maximum context length is 8192 tokens. However, your messages resulted in 12000 tokens.",
            type: 'invalid_request_error',
          },
        })
        return
      case 'auth-error':
        json(res, 401, {
          error: {
            message: 'Invalid API key provided.',
            type: 'authentication_error',
          },
        })
        return
      case 'consent-refusal':
        json(res, 400, {
          error: {
            message: 'The remote endpoint is not acknowledged. Confirm in settings.',
            type: 'invalid_request_error',
          },
        })
        return
      case 'empty-response':
        if (wantStream) {
          this.streamResponse(res, '')
        } else {
          this.jsonCompletion(res, '')
        }
        return
    }

    // Default success: generate content based on the request
    const content = this.generateContent(body)
    if (wantStream) {
      this.streamResponse(res, content)
    } else {
      this.jsonCompletion(res, content)
    }
  }

  private generateContent(body: Record<string, unknown>): string {
    const fmt = body.response_format as Record<string, unknown> | undefined
    const messages = (body.messages ?? []) as Array<{
      role: string
      content: string
    }>
    const systemContent = messages
      .filter((m) => m.role === 'system')
      .map((m) => m.content)
      .join(' ')
      .toLowerCase()

    // JSON schema request — generate matching content.
    // Order matters: flashcard/quiz prompts also contain "topic", so check the
    // more specific patterns first.
    if (fmt?.type === 'json_schema' || fmt?.type === 'json_object') {
      if (
        systemContent.includes('flashcard') ||
        (systemContent.includes('write') && systemContent.includes('cards'))
      ) {
        const countMatch = systemContent.match(/write (\d+) cards/)
        const topicMatch = systemContent.match(/for the topic "([^"]+)"/)
        const topic = topicMatch?.[1] ?? 'Thermodynamics Fundamentals'
        return cannedFlashcards(countMatch ? parseInt(countMatch[1]) : 4, topic)
      }
      if (systemContent.includes('quiz') || systemContent.includes('-question')) {
        const countMatch = systemContent.match(/(\d+)-question/)
        const diffMatch = systemContent.match(/at (basic|intermediate|exam) difficulty/)
        return cannedQuiz(countMatch ? parseInt(countMatch[1]) : 10, diffMatch?.[1] ?? 'basic')
      }
      if (systemContent.includes('topic')) return CANNED_TOPICS
      return JSON.stringify({ result: 'Acceptance fixture default JSON.' })
    }

    // Plain text response for chat
    return (
      'This is a deterministic response from the acceptance tutor fixture. ' +
      'The first law of thermodynamics states that energy cannot be created or destroyed.'
    )
  }

  /* ---- streaming helpers ----------------------------------------- */

  private streamResponse(res: ServerResponse, content: string) {
    res.writeHead(200, {
      'content-type': 'text/event-stream',
      'cache-control': 'no-cache',
      'connection': 'keep-alive',
    })

    const id = `chatcmpl-acceptance-${Date.now()}`

    // Role chunk
    res.write(sseChunk(id, { role: 'assistant', content: '' }))

    // Content chunks — split into small pieces for realistic streaming
    const words = content.split(' ')
    for (let i = 0; i < words.length; i++) {
      const text = (i > 0 ? ' ' : '') + words[i]
      res.write(sseChunk(id, { content: text }))
    }

    // Finish
    res.write(sseChunk(id, {}, 'stop'))
    res.write('data: [DONE]\n\n')
    res.end()
  }

  private streamPartialThenDisconnect(res: ServerResponse) {
    res.writeHead(200, {
      'content-type': 'text/event-stream',
      'cache-control': 'no-cache',
    })
    const id = `chatcmpl-partial-${Date.now()}`
    res.write(sseChunk(id, { role: 'assistant', content: '' }))
    res.write(sseChunk(id, { content: 'This response will be cut' }))
    // Destroy without [DONE]
    res.destroy()
  }

  private streamWithMidError(res: ServerResponse) {
    res.writeHead(200, {
      'content-type': 'text/event-stream',
      'cache-control': 'no-cache',
    })
    const id = `chatcmpl-error-${Date.now()}`
    res.write(sseChunk(id, { role: 'assistant', content: '' }))
    res.write(sseChunk(id, { content: 'Partial content before' }))
    res.write(
      `data: ${JSON.stringify({ error: { message: 'Injected mid-stream error', type: 'server_error' } })}\n\n`,
    )
    res.end()
  }

  private jsonCompletion(res: ServerResponse, content: string) {
    json(res, 200, {
      id: `chatcmpl-acceptance-${Date.now()}`,
      object: 'chat.completion',
      choices: [
        {
          message: { role: 'assistant', content },
          index: 0,
          finish_reason: 'stop',
        },
      ],
      usage: {
        prompt_tokens: 100,
        completion_tokens: content.length,
        total_tokens: 100 + content.length,
      },
    })
  }
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve) => {
    const chunks: Buffer[] = []
    req.on('data', (c: Buffer) => chunks.push(c))
    req.on('end', () => resolve(Buffer.concat(chunks).toString()))
  })
}

function json(res: ServerResponse, status: number, body: unknown) {
  const text = JSON.stringify(body)
  res.writeHead(status, {
    'content-type': 'application/json',
    'content-length': Buffer.byteLength(text),
  })
  res.end(text)
}

function sseChunk(id: string, delta: Record<string, string>, finishReason?: string): string {
  return `data: ${JSON.stringify({
    id,
    object: 'chat.completion.chunk',
    choices: [
      {
        index: 0,
        delta,
        finish_reason: finishReason ?? null,
      },
    ],
  })}\n\n`
}
