import type { BrandName, Offer } from './config'
import type { PurchaseErrorKind } from './pool/machine'
import { INDEX_NOUNS, type Action, type KindNouns, type Situation } from './pool/view'

/**
 * Every word a buyer reads, once per brand.
 *
 * One product under two names. The index sells seats in a pool that buys an
 * edition of the Route Index; the travel line sells seats on a sample trip
 * whose trip file is released when it fills. The escrow app, the release
 * condition and every rule a buyer is told about are the same, so the two
 * tables say the same things -- in the index's words, and in a trip's.
 *
 * The index's entries are the sentences these screens have always shown,
 * verbatim, and its tests pin them. The travel entries follow the rules the
 * travel line's copy was written under: it says "trip" where the index says
 * "pool", and "trip file" where it says "edition"; it never speaks of a
 * booking, a ticket or a discount; and nothing in it names a data source,
 * which reaches the page only through the offer.
 *
 * A total map per brand, typed, so a screen that gains a sentence fails to
 * compile until both brands have one. Sentences that are the same in both and
 * say nothing about the product -- how a refund claim works, what a wallet
 * that cannot pay a fee should do -- stay in their components.
 */

/** How a stamp draws an agreement: in ink while held, or in an outcome's colour. */
export type Outcome = 'held' | 'release' | 'refund'

export interface StampCopy {
  word: string
  outcome: Outcome
}

/** A sentence around one in-app link, whose text is the link's own. */
export interface Linked {
  before: string
  link: (id: string) => string
  after: string
}

interface CommonCopy {
  /** The nouns messages built outside the screens use: see `KindNouns`. */
  nouns: KindNouns
  /** The released file's name stem: `edition-N.json`, `trip-N.json`. */
  fileStem: 'edition' | 'trip'
  shell: {
    /** The way home: the logo's alt text, or the wordmark's text. */
    home: string
    source: string
    notFound: { heading: string; link: string }
  }
  /** The fallback for a buyer who arrived without their link. */
  mySeats: {
    heading: string
    find: string
    searching: string
    unavailable: string
    none: string
    item: (id: string) => string
  }
  /**
   * What a seat buys: paragraphs on the index, numbered steps on a trip,
   * which states its seat count where the index does not. The count is the
   * quote's, once one has been read -- never the offer's travellers -- and
   * undefined until then.
   */
  terms: {
    heading: string
    ordered: boolean
    items: (travellers: number | undefined) => string[]
  }
  purchase: {
    quote: string
    quoting: string
    noneOpen: string
    chainUnreachable: string
    notQuoting: string
    checkAgain: string
    confirm: {
      heading: string
      pool: string
      price: string
      seatsTaken: string
      deadline: string
      commitSha256: string
      committed: string
      buy: (price: string) => string
      connect: string
      recheck: string
      fresh: string
    }
    checking: string
    settling: string
    outcome: Record<PurchaseErrorKind, string>
    awaitingSeat: Linked
  }
  pool: {
    opening: string
    title: (id: string) => string
    readIncompatible: string
    readFailed: string
    firstReadIncompatible: (id: string) => string
    firstReadFailed: (id: string) => string
    firstReading: (id: string) => string
    waiting: {
      heading: string
      seats: (seats: number, of: number) => string
      unfilled: string
    }
    awaitingDelivery: {
      heading: string
      filled: (seats: number, of: number) => string
      unreleased: (deadline: string) => string
    }
    released: {
      heading: string
      closed: string
      open: string
      againstClosed: string
      againstOpen: string
      noLink: string
      lead: string
      check: { before: string; after: (against: string) => string }
    }
    refundable: { heading: string; missed: string; notStarted: string }
    refunding: {
      heading: string
      missed: string
      passAt: (cursor: number, seats: number) => string
    }
    refunded: {
      closedHeading: string
      closed: string
      heading: string
      passDone: string
    }
    gone: { heading: string; body: (id: string) => string; unknown: string }
    countdown: {
      passed: (when: string) => string
      left: (remaining: string, when: string) => string
      estimate: string
    }
    commitment: string
    seat: {
      confirming: string
      noWallet: string
      none: string
      refunded: (amount: string) => string
      toRefund: (cursor: number, seats: number) => string
      holds: string
    }
    stayOptedIn: string
    pauseProof: string
  }
  stamps: Record<Situation, StampCopy | null>
  calls: Record<Action, { button: string; phrase: string }>
  callsNote: string
  /** The link to the pool whose call holds the lock. */
  callElsewhere: (id: string) => string
}

export interface IndexCopy extends CommonCopy {
  brand: 'index'
  hero: { headline: string; x402: string; lede: string; poolLine: string }
}

export interface TravelCopy extends CommonCopy {
  brand: 'travel'
  wordmark: { name: string; line: string }
  badge: string
  /** The product line, verbatim and whole. */
  tagline: string
  hero: {
    /** The heading when the build carries no offer. */
    untitled: string
    sub: (offer: Offer, depart: string, back: string) => string
    /** How many must join: the quote's count, undefined before one is read. */
    hook: (travellers: number | undefined) => string
  }
  offerCard: {
    label: string
    route: (offer: Offer) => string
    fare: (offer: Offer) => string
    caption: (provider: string | undefined) => string
    seat: (price: string) => string
    bookBy: (when: string) => string
  }
  demoNotice: {
    label: string
    body: (source: string, provider: string | undefined) => string
    /** The source line when the build carries no offer. */
    neutralSource: string
  }
  testnetFunds: {
    heading: string
    listLabel: string
    steps: (assetId: string) => FundsStep[]
  }
  /** A trip past its deadline that nobody has expired yet. */
  lapsed: {
    heading: (travellers: number, filled: boolean) => string
    body: string
    noFee: string
    stamp: StampCopy
  }
}

export interface FundsStep {
  before: string
  link?: { href: string; text: string }
  after: string
}

export type Copy = IndexCopy | TravelCopy

/**
 * The product line. The index splits it between its headline and its lede;
 * the travel line sets it whole, as a tagline under the header.
 */
const LEDE =
  'Earnest holds the payment on Algorand until the condition is met: enough buyers ' +
  'joined to fund a purchase together, the deliverable matches what was promised, or both.'

const PURCHASE_OUTCOME: Record<PurchaseErrorKind, string> = {
  // No purchase was attempted, so none is said to have failed.
  quote_failed: 'Could not get a seat price.',
  // The reducer's message says the pool moved, that nothing was signed, and
  // how it moved.
  quote_stale: 'The purchase did not complete.',
  quote_unverifiable:
    'Could not check this quote against the chain, so nothing was signed. You can try buying again.',
  // Never an invitation to retry at once. This kind also covers a reply lost
  // after the paid request left, when the money may have moved; the route sells
  // whichever edition is open, so a buyer who simply tried again once the next
  // pool opened could pay twice.
  settle_failed:
    "The purchase did not complete, or its answer was lost. Check this wallet's USDC balance " +
    "and this pool's page before paying again.",
  receipt_unreadable:
    'Your payment went through, but its receipt could not be read. Do not pay again.',
}

const COUNTDOWN: CommonCopy['pool']['countdown'] = {
  passed: (when) => `The deadline, ${when}, has passed.`,
  left: (remaining, when) => `About ${remaining} left, until ${when}.`,
  estimate:
    'This is an estimate from the last read of the chain: the escrow app checks the deadline ' +
    "against the chain's own clock, which trails the clock on this device.",
}

const PAUSE_PROOF =
  'Refunds cannot be paused: no refund path in the escrow app checks its pause switch, so no ' +
  'operator can hold the money back.'

const INDEX: IndexCopy = {
  brand: 'index',
  nouns: INDEX_NOUNS,
  fileStem: 'edition',
  shell: {
    home: 'Earnest',
    source: 'Source on GitHub',
    notFound: { heading: 'No such page', link: 'Go to the start page' },
  },
  hero: {
    headline: '"If this, then pay"',
    x402: ' for x402',
    lede: LEDE,
    poolLine: 'Nobody pays unless enough do.',
  },
  mySeats: {
    heading: 'Your seats',
    find: 'Find my seats',
    searching: "Searching this wallet's history.",
    unavailable:
      "Could not search this wallet's history right now. Open the link from your purchase.",
    none:
      'No seat purchase from this wallet was found. A recent one may not be listed yet: open ' +
      'the link from your purchase.',
    item: (id) => `Pool ${id}`,
  },
  terms: {
    heading: 'What a seat buys',
    ordered: false,
    items: () => [
      'A seat joins a pool that buys one edition of the Algorand x402 Route Index: every ' +
        'Algorand USDC x402 route probed live, with its catalogued price beside the price it ' +
        'actually quotes.',
      'Your USDC is held by an Algorand escrow app until the edition whose sha256 was ' +
        'committed before you paid is delivered. It is released against matching bytes, and ' +
        'refunded on-chain if the seats do not fill or the edition is not delivered by the ' +
        'deadline.',
      'One seat per address: a wallet that already holds a seat in this pool cannot buy a ' +
        'second one. Machine-verified only: no arbitration, no human in the loop.',
    ],
  },
  purchase: {
    quote: 'Quote a seat in the open edition',
    quoting: "Asking for the open edition's seat price.",
    noneOpen: 'No edition is open right now.',
    chainUnreachable: 'Could not reach the chain. Try again.',
    notQuoting: 'The seat route is not quoting right now.',
    checkAgain: 'Check again',
    confirm: {
      heading: 'Confirm your seat',
      pool: 'Pool',
      price: 'Seat price',
      seatsTaken: 'Seats taken',
      deadline: 'Deadline',
      commitSha256: 'Committed sha256',
      committed:
        'This sha256 is already committed on chain, before you pay. The edition is released ' +
        'only against bytes that match it, so it is what you check the delivery against.',
      buy: (price) => `Buy a seat for ${price}`,
      connect: 'Connect a wallet to buy this seat.',
      recheck: 'Buying checks this quote against the chain once more before anything is signed.',
      fresh: 'Get a fresh quote',
    },
    checking: 'Checking the quote against the chain. Nothing has been signed.',
    settling: 'Waiting for your wallet to sign, and for the payment to settle.',
    outcome: PURCHASE_OUTCOME,
    awaitingSeat: {
      before: 'Your payment for a seat in ',
      link: (id) => `pool ${id}`,
      after: ' went through. Do not pay again: the seat appears once the chain catches up.',
    },
  },
  pool: {
    opening: 'Opening the pool.',
    title: (id) => `Pool ${id}`,
    readIncompatible:
      'The latest read of this pool could not be understood by this page, and it ' +
      'will not try again, so this shows the last one it could read.',
    readFailed: 'The latest read of this pool failed, so this shows the one before it.',
    firstReadIncompatible: (id) =>
      `This page cannot read pool ${id}, and trying again will not change that: this build of ` +
      'the page does not match the chain it is reading, so it has to be fixed by whoever runs ' +
      'it.',
    firstReadFailed: (id) => `Could not read pool ${id} from the chain. Trying again shortly.`,
    firstReading: (id) => `Reading pool ${id} from the chain.`,
    waiting: {
      heading: 'Waiting for seats',
      seats: (seats, of) => `${seats} of ${of} seats taken.`,
      unfilled: 'If the seats do not fill by the deadline, the pool is refunded on-chain.',
    },
    awaitingDelivery: {
      heading: 'Filled: the edition is owed',
      filled: (seats, of) =>
        `${seats} of ${of} seats taken, so the pool is filled. The edition is owed against ` +
        'the sha256 committed before anyone paid, and is released only against bytes that ' +
        'match it.',
      unreleased: (deadline) =>
        `If it is not delivered by the deadline, ${deadline}, the pool is refunded on-chain.`,
    },
    released: {
      heading: 'Delivered',
      closed:
        'The edition was delivered and the pool released. Its record has since been closed, so ' +
        'this page can no longer show the sha256 it was released against. That value stays in ' +
        "the chain's history, in the transactions that created and released the pool.",
      open: 'The edition was delivered, and the pool released against bytes matching this sha256.',
      againstClosed: 'the one the pool committed to',
      againstOpen: 'the committed sha256 above',
      noLink: 'The pool was released, but the link to the edition cannot be read right now.',
      lead: 'The edition: ',
      check: {
        before: 'To check it yourself, download ',
        after: (against) =>
          `, the file the pool committed to, and compare its sha256 with ${against}.`,
      },
    },
    refundable: {
      heading: 'The pool missed: refunds are due',
      missed: 'The pool did not complete, so the money held for its seats is coming back.',
      notStarted: 'The refund pass has not started yet.',
    },
    refunding: {
      heading: 'The pool missed: refunds are being sent',
      missed: 'The pool did not complete, so the money held for its seats is coming back.',
      passAt: (cursor, seats) => `The refund pass has been through ${cursor} of ${seats} seats.`,
    },
    refunded: {
      closedHeading: 'Refunded',
      closed:
        'The pool did not complete, and every seat was refunded. Its record has since been ' +
        'closed.',
      heading: 'The pool missed: the refund pass has finished',
      passDone: 'The refund pass has been through every seat.',
    },
    gone: {
      heading: 'No record on chain',
      body: (id) =>
        `Pool ${id} has no record on chain. A pool's record is removed when it is closed, so ` +
        'this pool has ended and been closed, or it never existed.',
      unknown: "Which way it ended could not be read from the chain's history.",
    },
    countdown: COUNTDOWN,
    commitment: 'Committed sha256: ',
    seat: {
      confirming:
        'Confirming your seat: the chain has not shown it yet. This page checks again shortly.',
      noWallet: 'Connect a wallet to see whether it holds a seat in this pool.',
      none: 'This wallet holds no seat in this pool.',
      refunded: (amount) =>
        `This wallet's seat has been refunded: ${amount} was paid back to this address.`,
      toRefund: (cursor, seats) =>
        "This wallet's seat is still to be refunded. The refund pass has been through " +
        `${cursor} of ${seats} seats.`,
      holds: 'This wallet holds a seat in this pool.',
    },
    stayOptedIn: 'Do not opt out of USDC while a pool you joined is live.',
    pauseProof: PAUSE_PROOF,
  },
  // One word per situation, so a stamp says only what is true of every pool in
  // that situation: the three refund situations share "Missed" -- see Stamp.
  stamps: {
    waiting: { word: 'Held', outcome: 'held' },
    'awaiting-delivery': { word: 'Held', outcome: 'held' },
    released: { word: 'Released', outcome: 'release' },
    refundable: { word: 'Missed', outcome: 'refund' },
    refunding: { word: 'Missed', outcome: 'refund' },
    refunded: { word: 'Missed', outcome: 'refund' },
    gone: null,
  },
  calls: {
    expire: { button: 'Expire the pool', phrase: 'the call to expire the pool' },
    refund_next: { button: 'Send the next refunds', phrase: 'the call to send the next refunds' },
    claim_refund: { button: 'Claim my refund', phrase: 'the call to claim your refund' },
    close: { button: 'Close the pool', phrase: 'the call to close the pool' },
  },
  callsNote:
    'Anyone may send these calls, and whoever sends one pays its network fee. A refund is ' +
    "always paid to the seat's address, never to whoever sends the call.",
  callElsewhere: (id) => `pool ${id}`,
}

/**
 * "3 travellers", "1 traveller", or "enough travellers" before a quote or the
 * agreement has said how many.
 */
const travellersOf = (count: number | undefined) =>
  count === undefined ? 'enough travellers' : count === 1 ? '1 traveller' : `${count} travellers`

/** "Each of the 3 seats", "Each of the seats", or "The seat" when there is one. */
const eachSeatOf = (count: number | undefined) =>
  count === undefined ? 'Each of the seats' : count === 1 ? 'The seat' : `Each of the ${count} seats`

/** Who has to take a seat for the trip to go ahead. */
const takersOf = (count: number | undefined) =>
  count === undefined
    ? 'enough different travellers take'
    : count === 1
      ? '1 traveller takes'
      : `${count} different travellers take`

const TRAVEL: TravelCopy = {
  brand: 'travel',
  nouns: { pool: 'trip', product: 'trip', file: 'trip file' },
  fileStem: 'trip',
  shell: {
    home: 'Earnest Travel',
    source: 'Source on GitHub',
    notFound: { heading: 'No such page', link: 'Go to the start page' },
  },
  wordmark: { name: 'Earnest', line: 'Travel' },
  badge: 'TestNet demo',
  tagline: `"If this, then pay" for x402. ${LEDE}`,
  hero: {
    untitled: 'A sample group trip',
    sub: (offer, depart, back) =>
      `${offer.nights} ${offer.nights === 1 ? 'night' : 'nights'} · ${depart} – ${back} · ` +
      `a trip for ${offer.travellers} ${offer.travellers === 1 ? 'traveller' : 'travellers'}`,
    hook: (travellers) =>
      `This trip only runs if ${travellersOf(travellers)} ${travellers === 1 ? 'joins' : 'join'}. ` +
      'Nobody pays unless enough do.',
  },
  offerCard: {
    label: 'Trip offer',
    route: (offer) =>
      `From ${offer.origin} (${offer.originIata}) to ${offer.city} (${offer.iata})`,
    fare: (offer) =>
      `Reference fare: ${offer.fare.amount} ${offer.fare.currency}, ${offer.fare.basis}`,
    caption: (provider) =>
      provider === undefined
        ? 'Fare data: see the demo notice'
        : `Fare data: ${provider}, see the demo notice`,
    seat: (price) => `Seat: ${price}`,
    bookBy: (when) => `Book by: ${when}`,
  },
  demoNotice: {
    label: 'Demo notice',
    body: (source, provider) =>
      [
        'A technical demonstration on Algorand TestNet, paid with test USDC. Nothing is booked ' +
          'or sold, no travel takes place, and the trip file cannot be exchanged for travel or ' +
          'anything else.',
        source,
        'The seat price is not a share of the fare.',
        ...(provider === undefined
          ? []
          : [`Earnest Travel is an independent demo, not a ${provider} product.`]),
        'A real group trip would be sold by a licensed organiser as merchant of record.',
      ].join(' '),
    neutralSource: 'Sample built from travel-API test data: indicative, not bookable.',
  },
  testnetFunds: {
    heading: 'Paying on TestNet',
    listLabel: 'How to get TestNet funds for a seat',
    steps: (assetId) => [
      {
        before:
          'Pera (tested; Defly untested): Settings › Developer settings › Node settings › ' +
          'TestNet, then connect. If you switched after connecting, disconnect and reconnect.',
        after: '',
      },
      {
        before: 'Get TestNet ALGO at ',
        link: {
          href: 'https://lora.algokit.io/testnet/fund',
          text: 'lora.algokit.io/testnet/fund',
        },
        after: ' (sign-in and captcha); keep at least 0.3 ALGO.',
      },
      { before: `Add asset ${assetId} (USDC).`, after: '' },
      {
        before: '',
        link: { href: 'https://faucet.circle.com', text: 'faucet.circle.com' },
        after:
          " → Algorand Testnet: 20 USDC every 2 h. The seat payment's network fee is currently " +
          'paid by the facilitator.',
      },
    ],
  },
  lapsed: {
    heading: (travellers, filled) =>
      filled
        ? 'Expired: the trip file was not released in time'
        : travellers === 1
          ? 'Expired: no traveller joined in time'
          : `Expired: fewer than ${travellers} travellers joined in time`,
    body:
      'The trip does not go ahead. Your USDC stays in the escrow app until someone sends the ' +
      'expire call below. Anyone can, and the refunds then go on-chain to each wallet that paid.',
    noFee:
      'No expire call is offered here, because this page has no wallet that can pay its ' +
      'network fee. Anyone can send it from any wallet, and the refunds still go on-chain to ' +
      'each wallet that paid.',
    stamp: { word: 'Expired', outcome: 'refund' },
  },
  mySeats: {
    ...INDEX.mySeats,
    item: (id) => `Trip ${id}`,
  },
  terms: {
    heading: 'How this trip works',
    ordered: true,
    items: (travellers) => [
      `${eachSeatOf(travellers)} is paid in USDC and held by an Algorand escrow app until ` +
        'the trip fills.',
      `If ${takersOf(travellers)} a seat ` +
        'before the deadline, every traveller receives the trip file: a file whose sha256 was ' +
        'committed on chain before anyone paid, released only against bytes that match it.',
      "If the trip doesn't fill, or the trip file isn't released by the deadline, every seat is " +
        'refunded on-chain to the wallet that paid.',
      'One seat per wallet: a wallet that already holds a seat on this trip cannot buy a second ' +
        'one. Objectively verifiable conditions only: no arbitration, no human in the loop.',
    ],
  },
  purchase: {
    ...INDEX.purchase,
    quote: 'See the seat price',
    quoting: "Asking for the trip's seat price.",
    noneOpen: 'No trip is open right now.',
    confirm: {
      ...INDEX.purchase.confirm,
      heading: 'Check your seat before paying',
      pool: 'Trip',
      committed:
        'This sha256 is already committed on chain, before you pay. The trip file is released ' +
        'only against bytes that match it, so it is what you check the trip file against.',
      buy: (price) => `Take a seat for ${price}`,
      connect: 'Connect a wallet to take this seat.',
      recheck:
        'Taking a seat checks this quote against the chain once more before anything is signed.',
    },
    outcome: {
      ...PURCHASE_OUTCOME,
      settle_failed:
        "The purchase did not complete, or its answer was lost. Check this wallet's USDC " +
        "balance and this trip's page before paying again.",
    },
    awaitingSeat: { ...INDEX.purchase.awaitingSeat, link: (id) => `trip ${id}` },
  },
  pool: {
    ...INDEX.pool,
    opening: 'Opening the trip.',
    title: (id) => `Trip ${id}`,
    readIncompatible:
      'The latest read of this trip could not be understood by this page, and it ' +
      'will not try again, so this shows the last one it could read.',
    readFailed: 'The latest read of this trip failed, so this shows the one before it.',
    firstReadIncompatible: (id) =>
      `This page cannot read trip ${id}, and trying again will not change that: this build of ` +
      'the page does not match the chain it is reading, so it has to be fixed by whoever runs ' +
      'it.',
    firstReadFailed: (id) => `Could not read trip ${id} from the chain. Trying again shortly.`,
    firstReading: (id) => `Reading trip ${id} from the chain.`,
    waiting: {
      heading: 'Waiting for travellers',
      seats: (seats, of) => `${seats} of ${of} seats taken.`,
      unfilled: "If the trip doesn't fill by the deadline, every seat is refunded on-chain.",
    },
    awaitingDelivery: {
      heading: 'Trip filled: the trip file is owed',
      filled: (seats, of) =>
        `${seats} of ${of} seats taken, so the trip is filled. The trip file is owed against ` +
        'the sha256 committed before anyone paid, and is released only against bytes that ' +
        'match it.',
      unreleased: (deadline) =>
        `If it is not released by the deadline, ${deadline}, every seat is refunded on-chain.`,
    },
    released: {
      heading: 'Trip file released',
      closed:
        'The trip file was released. Its record has since been closed, so this page can no ' +
        "longer show the sha256 it was released against. That value stays in the chain's " +
        'history, in the transactions that created the trip and released its file.',
      open: 'The trip file was released against bytes matching this sha256.',
      againstClosed: 'the one the trip committed to',
      againstOpen: 'the committed sha256 above',
      noLink: 'The trip file was released, but its link cannot be read right now.',
      lead: 'Your trip file: ',
      check: {
        before: 'To check it yourself, download ',
        after: (against) => ` (the committed file) and compare its sha256 with ${against}.`,
      },
    },
    refundable: {
      heading: 'Expired: refunds are due',
      missed: 'The trip did not go ahead, so the money held for its seats is coming back.',
      notStarted: 'The refund pass has not started yet.',
    },
    refunding: {
      heading: 'Expired: refunds are being sent',
      missed: 'The trip did not go ahead, so the money held for its seats is coming back.',
      passAt: INDEX.pool.refunding.passAt,
    },
    refunded: {
      closedHeading: 'Refunded',
      closed:
        'The trip did not go ahead, and every seat was refunded. Its record has since been ' +
        'closed.',
      heading: 'Expired: the refund pass has finished',
      passDone: 'The refund pass has been through every seat.',
    },
    gone: {
      heading: 'No record on chain',
      body: (id) =>
        `Trip ${id} has no record on chain. A trip's record is removed when it is closed, so ` +
        'this trip has ended and been closed, or it never existed.',
      unknown: INDEX.pool.gone.unknown,
    },
    seat: {
      ...INDEX.pool.seat,
      noWallet: 'Connect a wallet to see whether it holds a seat on this trip.',
      none: 'This wallet holds no seat on this trip.',
      holds: 'This wallet holds a seat on this trip.',
    },
    stayOptedIn: 'Do not opt out of USDC while a trip you joined is live.',
  },
  // Held while the money is in the escrow app, Released when the trip file
  // was, and Expired for every way a trip ends in refunds.
  stamps: {
    waiting: { word: 'Held', outcome: 'held' },
    'awaiting-delivery': { word: 'Held', outcome: 'held' },
    released: { word: 'Released', outcome: 'release' },
    refundable: { word: 'Expired', outcome: 'refund' },
    refunding: { word: 'Expired', outcome: 'refund' },
    refunded: { word: 'Expired', outcome: 'refund' },
    gone: null,
  },
  calls: {
    expire: { button: 'Expire the trip', phrase: 'the call to expire the trip' },
    refund_next: INDEX.calls.refund_next,
    claim_refund: INDEX.calls.claim_refund,
    close: { button: 'Close the trip', phrase: 'the call to close the trip' },
  },
  callsNote: INDEX.callsNote,
  callElsewhere: (id) => `trip ${id}`,
}

export const COPY: { index: IndexCopy; travel: TravelCopy } = { index: INDEX, travel: TRAVEL }

export function copyFor(brand: BrandName): Copy {
  return COPY[brand]
}
