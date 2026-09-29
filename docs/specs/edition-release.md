# Releasing an edition

> **Status: specification.** Written 2026-09-16.
> How an edition pool is released, and where the link to what it bought is recorded. An edition pool
> is a multi-seat `hash` agreement whose `commit_hash` is the `sha256` of the manifest's committed
> file: `edition-N.json` for the index, or `trip-N.json` for a trip file (§1.1). It is created by
> `agents/create_pool.py` from the edition's manifest. This document restates the contract facts it
> depends on rather than assuming `docs/specs/escrow-contract.md` has been read.

---

## 1. The decision

**An edition pool is released with the edition's CID in the note of the `release_hash`
transaction.** The note is the link's only on-chain record, and the only one this system keeps.

Two kinds of deliverable are sold through edition pools, and each has its own notes. The contract
knows nothing of kinds: it holds a `commit_hash`, and the notes are the only place the kind is
recorded.

| Kind | Committed file | Release note | Creation note |
|---|---|---|---|
| index | `edition-<N>.json` | `earnest:index:edition-<N>:agreement:<id>:cid:<cid>` | `earnest:index:edition-<N>:sha256:<hex>` |
| travel | `trip-<N>.json` | `earnest:travel:trip-<N>:agreement:<id>:cid:<cid>` | `earnest:travel:trip-<N>:sha256:<hex>` |

UTF-8, ASCII only, one line, no trailing newline. `agents/create_pool.py` writes the creation note
on the creating transaction, and the release note pairs with it, so one prefix per kind finds both
ends of every pool of that kind: `earnest:index:edition-` for the index, `earnest:travel:trip-` for
a trip file. Neither prefix is a prefix of the other, so a search for one kind never returns the
other's notes.

| Field | Form |
|---|---|
| `<N>` | the edition number from the manifest's `edition`, decimal, no leading zero. `edition` is the manifest's numbering key for both kinds, whatever its committed file is called |
| `<id>` | the agreement id, decimal, no leading zero |
| `<cid>` | the CID the pinning provider reported for the bundle, letters and digits only. When the bundle was pinned with more than one provider and they built different CIDs over the same bytes, the operator records one and the others stay off chain as further locators |

A reader accepts a note only if it matches, in full, the pattern of the kind it is looking for:

```
index:   ^earnest:index:edition-([1-9][0-9]*):agreement:(0|[1-9][0-9]*):cid:([A-Za-z0-9]+)$
travel:  ^earnest:travel:trip-([1-9][0-9]*):agreement:(0|[1-9][0-9]*):cid:([A-Za-z0-9]+)$
```

The groups mean the same in both: `<N>`, `<id>`, `<cid>`. The release tooling reads and writes
through one pattern that is the two as alternatives, each prefix taken whole, so a note that mixes
one kind's name with the other's stem, such as `earnest:index:trip-1:…`, matches neither.

A note is at most 1,024 bytes on Algorand. Either of these is about 110.

### 1.1 Which kind a manifest is

The tooling takes the kind from the manifest's `file`, which must match, in full,

```
^(edition|trip)-([1-9][0-9]*)\.json$
```

with the number equal to the manifest's `edition`. The stem is the kind: `edition` is the index,
`trip` a trip file. `agents/product_kinds.py` holds the one table both scripts read, so the name of
a committed file and the prefix of its notes cannot drift apart.

- **`agents/release_edition.py`** refuses any other `file`, including none, before it reads a key or
  a setting. The kind the manifest names is the kind of the release note; there is no flag to choose
  another.
- **`agents/create_pool.py`** takes a manifest with no `file` as the index's, and writes the index
  creation note for it, because a manifest carrying only `edition` and `sha256` is the form this
  script was first written against. A `file` that is present must match as above, or the script
  refuses before it reads a key, a setting or the chain: the creation note is written once and
  cannot be rewritten. Its dry run prints the note it would write.

## 2. Why the release transaction

- **No storage and nothing to keep in sync.** The chain is the record; there is no database row or
  repository file that could disagree with it.
- **It survives `close`.** `close` deletes the agreement and roster boxes, which are current state.
  A transaction's note, arguments and logs are history, which nothing deletes.
- **The payout and the link are one transaction.** A pool released through this path cannot be
  released without its link, because the call that moves the money is the call that carries it.
- **One search finds it.** The indexer's `/v2/transactions` search accepts an application id and a
  note prefix together, and honours both.

## 3. What the note is not

- **Not the commitment.** What the pool commits to is the `sha256` of its committed file,
  `edition-N.json` or `trip-N.json`, fixed at creation and compared by the contract on release. A
  CID depends on chunker, CID version, codec and layout, so the same bytes can legitimately have
  more than one, and a CID is a locator, never a commitment. What that rules out is treating a CID
  as one: deriving a CID from bytes, predicting one before anything is pinned, or checking a pin by
  whether it produced an expected value. The note does none of these. It records a CID only after
  the fact: the value the pinning provider reported for the bundle it was given, written after that
  bundle has been fetched back under it and hashed against the manifest (§4). A reader checks what
  it fetches the same way, by hashing it, never by comparing CIDs.
- **Not a living pointer.** It records the bundle delivered when the pool was released. A later
  rendering of the same edition is a new bundle with a new CID, and the release note does not move.
  Any directory carrying the committed file is authentically that edition.
- **Not a gateway.** No gateway host goes on chain. A reader resolves the CID against a gateway of
  its own choosing.
- **Not private.** A note is readable by anyone with access to the application's history. Once a
  pool is released, anyone can resolve its CID. So a rehearsal never releases a pool against a real
  edition's bundle: until an edition's MainNet pool is released, its CID is what a seat holder pays
  for, and a note on any public network gives it away.

## 4. Releasing

A release is irreversible. `release_hash` moves the agreement to `RELEASED`, pays the whole pool to
the beneficiary, and cannot be sent again. So everything below happens **before** the call, and a
failure of any of it sends nothing.

1. **The agreement is releasable.** Its state is `FUNDED`: every seat is sold. The signing key is
   the agreement's own `verifier`, the only sender the contract accepts. The chain's clock (the last
   block's timestamp, which the contract compares against) leaves enough time before the deadline
   to verify and submit.
2. **The manifest names the committed file.** The manifest's `sha256` equals the agreement's
   `commit_hash`, and the manifest's `files` entry for its `file` carries that same digest. Its
   `file` is one kind's committed file for its `edition` (§1.1), every name it inventories is a
   single path segment, and every digest is a lowercase sha256, so each file is fetched under the
   CID and nowhere else.
3. **The bundle is fetched and hashed, not assumed.** Every file the manifest inventories is fetched
   through public gateways under the CID and hashed against its published digest, the same check a
   buyer runs. The gateways are `RECHECK_GATEWAYS`, a comma-separated list of at least one, tried in
   order for each file; unset or blank, it is `https://ipfs.filebase.io`. Each entry is a bare host
   or an https origin (no path, query, fragment or credentials), and any other entry is refused. This is what makes a release honest. The contract only compares the submitted digest
   with `commit_hash`, which is already public, so a verifier could pass it straight back without
   delivering anything; the on-chain comparison is a record, not a proof.
4. **Only then, `release_hash(agreement_id, commit_hash)` with the note**, under the prefix of the
   kind the manifest's `file` names.
5. **The confirmed transaction is read back** and its note compared with the one sent.

**Step 3 can be skipped** with `--skip-gateway-check`, on any network, when the operator's machine
cannot reach the gateways. Steps 1, 2, 4 and 5 still run, and the note then records a CID nobody
fetched: only the operator's own check that the bundle is served stands behind the release.

No other path releases an edition pool. The generic operator CLI, `agents/pool_ops.py release`,
sends no note, and refuses a multi-seat agreement unless told explicitly that no edition note is
wanted, which is only ever true of a test pool.

**The verifier's key is kept apart from the server's configuration.** It is the key that decides
delivery happened, so the release path reads it from an operator file for the network the settings
name — `VERIFIER_MNEMONIC` in `contracts/.env.mainnet`, or in `contracts/.env.testnet` for a
rehearsal — and never from the `.env` the server loads. Choosing the file by network means a
TestNet configuration cannot sign with the MainNet key.

## 5. Reading

A reader looking for a pool's edition link:

1. Searches `/v2/transactions` with `application-id=<app id>`, `tx-type=appl` and
   `note-prefix=<base64 of the kind's prefix>`: `earnest:index:edition-` for the index,
   `earnest:travel:trip-` for a trip file.
2. Keeps a transaction only if it is an application call to that application whose first argument
   is the `release_hash` selector and whose second is the agreement id as eight big-endian bytes,
   and whose note matches its kind's pattern in §1 with the same `<id>`. The contract accepts
   `release_hash` only from the agreement's verifier and only once, so a committed call that passes
   these checks is the release.
3. Presents `<gateway>/ipfs/<cid>/<stem>-<N>.html`, and the committed `<stem>-<N>.json` beside it
   for the buyer's own check, where `<stem>` is `edition` for the index and `trip` for a trip file.

A note that fails any of these is ignored, never shown: the note prefix is not reserved, and anyone
can write a transaction whose note starts with it.
