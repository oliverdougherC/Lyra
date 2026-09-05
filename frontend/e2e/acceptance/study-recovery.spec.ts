/** Durable recovery through production components and real review/card routes. */
import { test, expect, type Page } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { join, resolve } from 'node:path'
import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  clearTutorState,
  createClass,
  enqueueTutorResponse,
  flipCard,
  rateCard,
  readAcceptanceState,
  uploadDocument,
  waitForDocumentReady,
  waitForStudyReady,
} from './helpers'

type Operation = { operation_id: string; rating: string }
type Card = {
  part_id: number
  card: { front: string; back: string; topic: string }
  card_state: { reps: number }
}

function gate() {
  let release!: () => void
  let released = false
  const promise = new Promise<void>((resolve) => {
    release = () => {
      released = true
      resolve()
    }
  })
  return { promise, release, done: () => released }
}

async function snapshot(page: Page, id: number) {
  return page.evaluate(
    (deckId) => JSON.parse(sessionStorage.getItem(`lyra:study-session:v1:${deckId}`)!),
    id,
  )
}

async function reviewOperations(partId: number): Promise<string[]> {
  const state = await readAcceptanceState()
  if (!state) throw new Error('Acceptance stack state is unavailable')
  return JSON.parse(
    execFileSync(
      'python3',
      [
        '-c',
        "import json, sqlite3, sys; c = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True); print(json.dumps([r[0] for r in c.execute('select op_id from card_review_log where part_id = ?', (int(sys.argv[2]),))]))",
        join(state.dataDir, 'lyra.db'),
        String(partId),
      ],
      { encoding: 'utf8' },
    ),
  )
}

test.describe('Durable study recovery', () => {
  let classId: number
  let documentId: number

  test.beforeAll(async () => {
    classId = (await createClass('Acceptance: durable recovery')).id
    const response = await uploadDocument(
      classId,
      resolve(__dirname, 'test-data/sample.txt'),
      'sample.txt',
    )
    documentId = (await response.json()).id
    await waitForDocumentReady(documentId)
  })
  test.afterEach(clearTutorState)

  test('PLA-477: unavailable storage can be retried without reloading or losing a pending operation', async ({
    page,
  }) => {
    const deck = await createDeck()
    let pending: Operation | undefined
    await page.route(
      `**/api/cards/${deck.cards[0].part_id}/review`,
      async (route) => {
        pending = route.request().postDataJSON()
        expect((await route.fetch()).ok()).toBeTruthy()
        await route.abort('failed')
      },
      { times: 1 },
    )
    await open(page, deck.id)
    await flipCard(page)
    await rateCard(page, 'Easy')
    await expect(page.getByText(/Easy review could not be confirmed/)).toBeVisible()
    await page.getByRole('link', { name: 'Practice', exact: true }).click()
    await expect(page.getByRole('tabpanel', { name: /Practice/ })).toBeVisible()
    await page.evaluate(() => {
      const getItem = Storage.prototype.getItem
      Object.assign(window, {
        restoreRecoveryStorage: () => {
          Storage.prototype.getItem = getItem
        },
      })
      Storage.prototype.getItem = function (key: string) {
        if (key.startsWith('lyra:study-session:'))
          throw new DOMException('Storage unavailable', 'SecurityError')
        return getItem.call(this, key)
      }
    })
    await page.getByRole('main').locator(`a[href$="/classes/${classId}/study/${deck.id}"]`).click()
    await expect(
      page.getByText('Could not restore this study session', { exact: true }),
    ).toBeVisible()
    await page.evaluate(() =>
      (window as unknown as { restoreRecoveryStorage: () => void }).restoreRecoveryStorage(),
    )
    await page.getByRole('button', { name: 'Retry storage access', exact: true }).click()
    await expect(page.getByText(/Easy review could not be confirmed/)).toBeVisible()
    const retry = page.waitForRequest((request) =>
      request.url().endsWith(`/api/cards/${deck.cards[0].part_id}/review`),
    )
    await rateCard(page, 'Easy')
    expect((await retry).postDataJSON()).toEqual(pending)
    await expect(page.getByText(/Card 2 of/)).toBeVisible()
    expect(
      (await (await apiPost(`/api/cards/${deck.cards[0].part_id}/review`, pending)).json()).reps,
    ).toBe(1)
  })

  test('PLA-477: failed acknowledgement storage write retains the committed operation for retry', async ({
    page,
  }) => {
    const deck = await createDeck()
    await open(page, deck.id)
    await page.evaluate(() => {
      const setItem = Storage.prototype.setItem
      Object.assign(window, {
        restoreRecoveryStorage: () => {
          Storage.prototype.setItem = setItem
        },
      })
      Storage.prototype.setItem = function (key: string, value: string) {
        if (key.startsWith('lyra:study-session:') && JSON.parse(value).operation === null)
          throw new DOMException('Storage quota exceeded', 'QuotaExceededError')
        setItem.call(this, key, value)
      }
    })
    const sent = page.waitForRequest((request) =>
      request.url().endsWith(`/api/cards/${deck.cards[0].part_id}/review`),
    )
    await flipCard(page)
    await rateCard(page, 'Easy')
    const pending = (await sent).postDataJSON()
    await expect(
      page.getByText('Could not restore this study session', { exact: true }),
    ).toBeVisible()
    const saved = await snapshot(page, deck.id)
    expect(saved.operation).toEqual({ id: pending.operation_id, rating: 'easy' })
    expect(saved.ratings.easy).toBe(0)
    await page.evaluate(() =>
      (window as unknown as { restoreRecoveryStorage: () => void }).restoreRecoveryStorage(),
    )
    await page.getByRole('button', { name: 'Retry storage access', exact: true }).click()
    await expect(page.getByText(/Easy review could not be confirmed/)).toBeVisible()
    const replay = page.waitForRequest((request) =>
      request.url().endsWith(`/api/cards/${deck.cards[0].part_id}/review`),
    )
    await rateCard(page, 'Easy')
    expect((await replay).postDataJSON()).toEqual(pending)
    await expect(page.getByText(/Card 2 of/)).toBeVisible()
    expect((await snapshot(page, deck.id)).ratings.easy).toBe(1)
    expect(
      (await (await apiPost(`/api/cards/${deck.cards[0].part_id}/review`, pending)).json()).reps,
    ).toBe(1)
  })

  test('PLA-477: malformed recovery remains intact while unrecorded study remains usable', async ({
    page,
  }) => {
    const deck = await createDeck()
    await open(page, deck.id)
    const malformed = '{"operation":{"id":"possibly-committed","rating":"easy"},"queue":broken'
    await page.evaluate(
      ({ id, value }) => sessionStorage.setItem(`lyra:study-session:v1:${id}`, value),
      { id: deck.id, value: malformed },
    )
    await page.reload()
    await expect(
      page.getByText('Could not restore this study session', { exact: true }),
    ).toBeVisible()
    let requests = 0
    page.on('request', (request) => {
      if (/\/api\/cards\/\d+\/review$/.test(request.url())) requests++
    })
    await page.getByRole('button', { name: 'Study without recording reviews', exact: true }).click()
    await expect(page.getByText(deck.cards[0].card.front, { exact: true })).toBeVisible()
    await flipCard(page)
    await page.getByRole('button', { name: 'Next card (not recorded)', exact: true }).click()
    await expect(page.getByText(deck.cards[1].card.front, { exact: true })).toBeVisible()
    expect(requests).toBe(0)
    expect(
      await page.evaluate((id) => sessionStorage.getItem(`lyra:study-session:v1:${id}`), deck.id),
    ).toBe(malformed)
    const authoritative = await (await apiGet(`/api/decks/${deck.id}`)).json()
    expect(authoritative.cards.map((card: Card) => card.card_state.reps)).toEqual([0, 0, 0])
  })

  async function createDeck(count = 3) {
    const perTopic = count > 6 ? 5 : count
    const topics = Array.from(
      { length: Math.ceil(count / perTopic) },
      (_, i) => `Thermodynamics ${i + 1}`,
    )
    await enqueueTutorResponse(JSON.stringify({ topics }))
    for (const [index, topic] of topics.entries()) {
      await enqueueTutorResponse(
        JSON.stringify({
          cards: Array.from({ length: perTopic }, (_, i) => ({
            front: `Recovery card ${index * perTopic + i + 1}: what is energy?`,
            back: `Energy answer ${index * perTopic + i + 1}.`,
            topic,
          })),
        }),
      )
    }
    const response = await apiPost(`/api/classes/${classId}/decks`, {
      title: `Durable recovery ${Date.now()}`,
      document_ids: [documentId],
      cards_per_topic: perTopic,
    })
    expect(response.status).toBe(202)
    const deck = await response.json()
    await waitForStudyReady('decks', deck.id)
    const session = await (await apiGet(`/api/decks/${deck.id}/session`)).json()
    expect(session.cards).toHaveLength(Math.min(count, 20))
    return { id: deck.id as number, cards: session.cards as Card[] }
  }

  async function open(page: Page, deckId: number) {
    await page.goto(`/classes/${classId}/study/${deckId}`)
    await expect(page.getByText(/Card 1 of/)).toBeVisible()
  }

  test('PLA-477: late A acknowledgement cannot replace unresolved B after remount and reload', async ({
    page,
  }) => {
    const deck = await createDeck()
    const [a, b] = deck.cards
    const committedA = gate()
    const releaseA = gate()
    const deliveredA = gate()
    let originalA: Operation | undefined
    let pendingB: Operation | undefined
    let aRequests = 0
    await page.route(`**/api/cards/${a.part_id}/review`, async (route) => {
      if (aRequests++ > 0) {
        await route.continue()
        return
      }
      originalA = route.request().postDataJSON()
      const response = await route.fetch()
      expect(response.ok()).toBeTruthy()
      committedA.release()
      await releaseA.promise
      await route.fulfill({ response })
      deliveredA.release()
    })
    await page.route(
      `**/api/cards/${b.part_id}/review`,
      async (route) => {
        pendingB = route.request().postDataJSON()
        const response = await route.fetch()
        expect(response.ok()).toBeTruthy()
        await route.abort('failed')
      },
      { times: 1 },
    )
    try {
      await open(page, deck.id)
      await flipCard(page)
      await rateCard(page, 'Easy')
      await expect.poll(committedA.done, { message: 'A reached the real review commit' }).toBe(true)
      // Real client-side navigation keeps the old request/continuation alive.
      await page.getByRole('link', { name: 'Practice', exact: true }).click()
      await expect(page.getByRole('tabpanel', { name: /Practice/ })).toBeVisible()
      await expect(page).toHaveURL(new RegExp(`/classes/${classId}\\?tab=practice$`))
      await page
        .getByRole('main')
        .locator(`a[href$="/classes/${classId}/study/${deck.id}"]`)
        .click()
      await expect(page.getByText(/Easy review could not be confirmed/)).toBeVisible()
      const replayA = page.waitForRequest((request) =>
        request.url().endsWith(`/api/cards/${a.part_id}/review`),
      )
      await rateCard(page, 'Easy')
      expect((await replayA).postDataJSON()).toEqual(originalA)
      await expect(page.getByText(/Card 2 of/)).toBeVisible()
      await flipCard(page)
      await rateCard(page, 'Good')
      await expect(page.getByText(/Good review could not be confirmed/)).toBeVisible()
      const beforeLateResponse = await snapshot(page, deck.id)
      expect(beforeLateResponse.operation.id).toBe(pendingB?.operation_id)
      const responseDelivered = page.waitForResponse((response) =>
        response.url().endsWith(`/api/cards/${a.part_id}/review`),
      )
      releaseA.release()
      await expect
        .poll(deliveredA.done, { message: 'Original A response was delivered' })
        .toBe(true)
      await responseDelivered
      // Let the original mutation continuation execute before inspecting external storage.
      await page.evaluate(
        () =>
          new Promise<void>((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
          ),
      )
      expect(await snapshot(page, deck.id)).toEqual(beforeLateResponse)
      await page.reload()
      await expect(page.getByText(/Good review could not be confirmed/)).toBeVisible()
      const replayB = page.waitForRequest((request) =>
        request.url().endsWith(`/api/cards/${b.part_id}/review`),
      )
      await rateCard(page, 'Good')
      expect((await replayB).postDataJSON()).toEqual(pendingB)
      await expect(page.getByText(/Card 3 of/)).toBeVisible()
      const saved = await snapshot(page, deck.id)
      expect(saved.ratings).toEqual({ again: 0, hard: 0, good: 1, easy: 1 })
      const authoritative = await (await apiGet(`/api/decks/${deck.id}`)).json()
      expect(
        authoritative.cards.find((card: Card) => card.part_id === a.part_id).card_state.reps,
      ).toBe(1)
      expect(
        authoritative.cards.find((card: Card) => card.part_id === b.part_id).card_state.reps,
      ).toBe(1)
      expect((await (await apiPost(`/api/cards/${b.part_id}/review`, pendingB)).json()).reps).toBe(
        1,
      )
      expect(await reviewOperations(a.part_id)).toEqual([originalA!.operation_id])
      expect(await reviewOperations(b.part_id)).toEqual([pendingB!.operation_id])
    } finally {
      releaseA.release()
    }
  })

  test('PLA-404: restored queue omits deleted cards and refreshes corrected content and scheduling', async ({
    page,
  }) => {
    const deck = await createDeck(4)
    await open(page, deck.id)
    await flipCard(page)
    await rateCard(page, 'Easy')
    await expect(page.getByText(/Card 2 of/)).toBeVisible()
    await page.getByRole('link', { name: 'Practice', exact: true }).click()
    await expect(page.getByRole('tabpanel', { name: /Practice/ })).toBeVisible()
    expect((await apiDelete(`/api/cards/${deck.cards[1].part_id}`)).ok).toBeTruthy()
    expect(
      (
        await apiPatch(`/api/cards/${deck.cards[2].part_id}`, {
          front: 'CORRECTED: energy is conserved',
          back: 'CORRECTED answer',
          topic: 'Corrected topic',
        })
      ).ok,
    ).toBeTruthy()
    expect(
      (
        await apiPost(`/api/cards/${deck.cards[2].part_id}/review`, {
          rating: 'good',
          operation_id: `external-${deck.id}`,
        })
      ).ok,
    ).toBeTruthy()
    await page.getByRole('main').locator(`a[href$="/classes/${classId}/study/${deck.id}"]`).click()
    await expect(page.getByText('CORRECTED: energy is conserved', { exact: true })).toBeVisible()
    const restored = await snapshot(page, deck.id)
    expect(restored.queue.map((card: Card) => card.part_id)).toEqual(
      deck.cards.slice(2).map((card) => card.part_id),
    )
    expect(restored.states.find(([id]: [number]) => id === deck.cards[2].part_id)[1].reps).toBe(1)
    expect(restored.ratings).toEqual({ again: 0, hard: 0, good: 0, easy: 1 })
    await flipCard(page)
    await rateCard(page, 'Good')
    await expect(page.getByText(deck.cards[3].card.front, { exact: true })).toBeVisible()
    await flipCard(page)
    await rateCard(page, 'Good')
    await expect(page.getByText('Session complete', { exact: true })).toBeVisible()
    await expect(page.getByText(/You reviewed 3 cards/)).toBeVisible()
  })

  for (const committed of [false, true]) {
    test(`PLA-404: removed pending card has honest continuation (committed=${committed})`, async ({
      page,
    }) => {
      const deck = await createDeck()
      let pending: Operation | undefined
      await page.route(
        `**/api/cards/${deck.cards[0].part_id}/review`,
        async (route) => {
          pending = route.request().postDataJSON()
          if (committed) expect((await route.fetch()).ok()).toBeTruthy()
          await route.abort('failed')
        },
        { times: 1 },
      )
      await open(page, deck.id)
      await flipCard(page)
      await rateCard(page, 'Easy')
      await expect(page.getByText(/Easy review could not be confirmed/)).toBeVisible()
      expect((await apiDelete(`/api/cards/${deck.cards[0].part_id}`)).ok).toBeTruthy()
      // A retry reaches the production 404 path before authoritative restoration.
      const failed = page.waitForResponse((response) =>
        response.url().endsWith(`/api/cards/${deck.cards[0].part_id}/review`),
      )
      await rateCard(page, 'Easy')
      expect((await failed).status()).toBe(404)
      await page.reload()
      await expect(
        page.getByRole('button', { name: 'Continue remaining cards', exact: true }),
      ).toBeVisible()
      await page.getByRole('button', { name: 'Continue remaining cards', exact: true }).click()
      await expect(page.getByText(deck.cards[1].card.front, { exact: true })).toBeVisible()
      const saved = await snapshot(page, deck.id)
      expect(JSON.stringify(saved.unresolved)).toContain(pending!.operation_id)
      expect(saved.ratings).toEqual({ again: 0, hard: 0, good: 0, easy: 0 })
      await page.reload()
      await expect(page.getByText(deck.cards[1].card.front, { exact: true })).toBeVisible()
      for (let i = 1; i < deck.cards.length; i++) {
        await flipCard(page)
        await rateCard(page, 'Good')
        if (i < deck.cards.length - 1)
          await expect(page.getByText(deck.cards[i + 1].card.front, { exact: true })).toBeVisible()
      }
      await expect(page.getByText(/You reviewed 2 cards/)).toBeVisible()
      const final = await (await apiGet(`/api/decks/${deck.id}`)).json()
      expect(final.cards.map((card: Card) => card.card_state.reps)).toEqual([1, 1])
    })
  }

  test('PLA-404: existing pending card omitted by the default 20-card response retains its operation', async ({
    page,
  }) => {
    const deck = await createDeck(25)
    const first = deck.cards[0]
    let pending: Operation | undefined
    await page.route(
      `**/api/cards/${first.part_id}/review`,
      async (route) => {
        pending = route.request().postDataJSON()
        expect((await route.fetch()).ok()).toBeTruthy()
        await route.abort('failed')
      },
      { times: 1 },
    )
    await open(page, deck.id)
    await flipCard(page)
    await rateCard(page, 'Easy')
    await expect(page.getByText(/Easy review could not be confirmed/)).toBeVisible()
    const limited = await (await apiGet(`/api/decks/${deck.id}/session`)).json()
    expect(limited.cards).toHaveLength(20)
    expect(limited.cards.some((card: Card) => card.part_id === first.part_id)).toBe(false)
    await page.reload()
    await expect(page.getByText(/Easy review could not be confirmed/)).toBeVisible()
    const replay = page.waitForRequest((request) =>
      request.url().endsWith(`/api/cards/${first.part_id}/review`),
    )
    await rateCard(page, 'Easy')
    expect((await replay).postDataJSON()).toEqual(pending)
    await expect(page.getByText(/Card 2 of 20/)).toBeVisible()
    const full = await (await apiGet(`/api/decks/${deck.id}`)).json()
    expect(full.cards.find((card: Card) => card.part_id === first.part_id).card_state.reps).toBe(1)
  })
})
