# Frozen editions of the Algorand x402 Route Index

What ships publicly for each edition: the free report, the extract behind it, and the manifest
carrying the `sha256` a pool commits to. How each figure is measured is set out at the top of the
free report, `edition-N-sample.html`, before any of the figures.

**The full edition is deliberately not here.** It is what a seat holder buys, and it is delivered
as an IPFS CID on release. The manifest is the part that has to be public *before* anyone pays,
because the digest is bound into the agreement at creation and cannot be changed afterwards.

The files in this directory are committed on purpose: they are the free tier, and they are meant
to be readable by anyone without running anything. Editions are produced, rendered and pinned by
tooling maintained in a separate, private repository; nothing in this repository runs it or
depends on it. Publishing an edition means committing its public files here, byte for byte as they
were rendered, and the full edition never lands in this tree.

## The two tiers

The free tier states the finding; the edition says who. The public files carry every aggregate
figure the edition supports, and the extract keeps the shape of a discrepancy: the catalogued
price, the price the route actually quoted, and whether it answered. Which routes those
measurements belong to is what a seat buys.

The extract carries those three columns and no others, which is a stronger rule than "with the
names removed". The catalogued price and the settle count are both served by the facilitator's
own public directory, and the distinct-payer count can be derived by anyone from the public
indexer -- so an extract carrying the last two could be joined back against those sources to
recover the payee without buying anything. On edition 1 that join pinned every row in the extract
to a single payee. Withholding them is what makes the redaction mean something.

The free report still shows the shape of the full edition: its table has every column the edition
carries, and each one outside those three reads `withheld`, identically on every row. The sample
JSON lists the same columns as `withheld_columns`.

| File | What it is |
|---|---|
| `edition-N-sample.html` | the free report: open it in a browser |
| `edition-N-sample.json` | the same summary and extract, for machines |
| `edition-N-manifest.json` | the digest of every file in the edition's bundle |

The host that sells the edition serves the same three files, byte for byte, under `/app/editions/`,
so the free report can be opened without cloning anything and every digest still holds for the
downloaded copy. Only these names are served: if you put a delivered `edition-N.json` in this
directory to check it, a deploy from the same tree does not publish it.

## Checking an edition yourself

The manifest hashes the files exactly as they were published, so every digest can be checked with
nothing but `sha256sum` and without trusting us.

Two of the three files below are in this directory. `edition-1.json` is not — it is the paid
artifact, so fetch it from the CID delivered on release and put it here first. Without it the
first command has nothing to hash; the other two work as they stand:

```bash
sha256sum edition-1.json        # must equal .sha256 in the manifest
sha256sum edition-1-sample.json # must equal .files["edition-1-sample.json"]
sha256sum edition-1-sample.html # must equal .files["edition-1-sample.html"]
```

The first line is the one that matters: `.sha256` is the value bound on chain as `commit_hash`
before anyone paid, so hashing the file you were delivered and comparing is the whole of checking
that you received what was promised.

The CID is on chain. It is the note of the transaction that released the edition's pool,
`earnest:index:edition-<N>:agreement:<id>:cid:<cid>`, and `docs/specs/edition-release.md` §5 says
how to find it.

The extract carries `sample_of` -- the full edition's digest -- so a reader can prove the slice
came out of the file being sold. Narrow the edition you bought to the columns `extract_rule`
names and every extract row is in it.

`extract_rule` states the selection in full: which rows it is drawn from, how they are ordered,
how ties are broken and the exact index taken at each step. It is written that way so a seat
holder can rebuild the published slice from the edition and get the same rows back -- a rule that
said only "sampled at even intervals" would leave two plausible readings, and a reader who got
different rows could not tell a sharpened rule from a doctored extract.

## What the on-chain hash covers

`commit_hash` is `sha256(edition-N.json)`: the dataset, not the bundle. The CSV and the reports
are renderings of it, listed in the manifest so a bundle can be checked file by file.

That is what lets the renderings improve for an edition already frozen, already sold and already
released: they are regenerated from the published file without touching it, and a re-rendering is
a new bundle of the same edition, not a new edition. **The CID identifies a bundle; the `sha256`
identifies the edition.** Any directory carrying the correct `edition-N.json` is authentically
that edition.

## Edition 1

| | |
|---|---|
| Probed | 2026-09-16T12:49:23Z |
| Rows | 1841 |
| `sha256` | `7ff9c6cc101dc4c53d6638f802fb3cd3e976339164194e45c0d804136eb0ddd9` |
| Full edition | 1,074,682 bytes, not committed -- delivered to seat holders as a CID on release |
| Free report | `https://earnest.lxfoundry.ai/app/editions/edition-1-sample.html` |

This run supersedes two earlier runs of edition 1, neither of which was bound to a pool:

- probed 2026-09-15T11:39:33Z (`sha256 16ac4ac0…`), pinned. It sent every route one GET and read
  only the `payment-required` header, so a route listed as POST, or one sending its terms in the
  body of its 402, was recorded as `no-402` -- 441 rows.
- probed 2026-09-16T11:11:00Z (`sha256 0c8f92eb…`), never pinned. It asked with the listed verb
  first, read terms from a header-less 402's body and paced its requests per host, and carried no
  settled volume.

This run does all of that and adds the four settled-volume columns read from the facilitator's
leaderboard: 148 rows are `no-402`, and 1,824 of the 1,841 endpoints carry a reported volume. Its
CSV and HTML are renderings of this dataset; nothing of the earlier runs is reused.

Measured reading up to 700 pages of each payee's receipts. A ceiling of 250 leaves the busiest
merchant uncountable: one payee's receipts run to 528 pages, and its distinct-payer count is still
climbing at page 250, so a ceiling that low publishes an empty cell for the merchants the column
is most interesting about.

Edition 1's free report was re-rendered after this run, from the same dataset, to change one
sentence of its "Checking it yourself" section. The dataset and its `sha256` are unchanged; the
manifest carries the new digest of `edition-1-sample.html`.

## Edition 2

| | |
|---|---|
| Probed | 2026-09-28T08:44:06Z |
| Rows | 2103 |
| `sha256` | `a40ab58f47a662d3126fa843a5038b81d2bd0ca82854f2625e6a49ac33e4f2fb` |
| Full edition | 1,224,960 bytes, not committed -- delivered to seat holders as a CID on release |
| Free report | `https://earnest.lxfoundry.ai/app/editions/edition-2-sample.html` |

To check edition 2, run the commands under *Checking an edition yourself* with `edition-2` in
place of `edition-1`, against `edition-2-manifest.json`.

## Edition 3

| | |
|---|---|
| Probed | 2026-10-02T15:18:07Z |
| Rows | 2257 |
| `sha256` | `3d4eaf9a7904af9b8990d0a20985e3698c5032af239d762277a3d62723d21e36` |
| Full edition | 1,319,680 bytes, not committed -- delivered to seat holders as a CID on release |
| Free report | `https://earnest.lxfoundry.ai/app/editions/edition-3-sample.html` |

To check edition 3, run the commands under *Checking an edition yourself* with `edition-3` in
place of `edition-1`, against `edition-3-manifest.json`.
