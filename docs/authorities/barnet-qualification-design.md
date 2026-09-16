# Barnet live qualification design

## Problem

Barnet's weekly discovery is resumable, but its checkpoint is not bound to an exact date range. The current-list route also stops at a source-reported result cap. A live bootstrap needs a terminal checkpoint for a 30-day scope, every older open application, and every active appeal before it can write a qualification receipt.

## Usage

Run a fresh qualification with an inclusive 30-day range and older-open discovery.

```text
uv run python scripts/qualify_barnet.py \
  --confirm-live \
  --data-dir .yimby/qualification-barnet-2026-09-16 \
  --start 2026-08-18 \
  --end 2026-09-16 \
  --include-open
```

If the portal interrupts the run, repeat the command with `--resume`. The command accepts non-empty state only with that flag.

## Shape

`BarnetDiscoveryScope` binds a checkpoint to its start date, end date, and older-open choice. The existing `BarnetCheckpointV1` keeps page progress, the ordered completed-query keys, and the references seen across overlapping searches.

The adapter owns four query families:

- Weekly validated and decided lists for every Monday that intersects the requested range.
- One received-date search bounded to the exact inclusive qualification range.
- Four native open-case statuses from Barnet's advanced form.
- Five native active-appeal statuses from Barnet's advanced form.

Private typed query objects produce one canonical ordered key inventory. The adapter validates the live form against that inventory before it submits a search. The qualification command imports the canonical inventory instead of repeating status strings.

Each query becomes complete only when its parsed rows reconcile with the portal's displayed total, displayed row span, and page markers. This rejects a replayed earlier page instead of counting it twice. The adapter yields every page with its next checkpoint so SQLite can commit references and progress together. A resumed page first recreates the server-side search session.

`BarnetQualificationReceiptV1` records the exact scope and query inventory, durable counts, first-pass and rerun costs, named checks, and two pending future refresh cycles. The command writes the receipt only after SQLite integrity, evidence hashes, exact durable reference agreement, section completeness, retry state, attachment policy, and an immediate zero-network rerun all pass.

## Synthesis decision

The direct adapter design won the architecture comparison. It follows the existing West Suffolk boundary and keeps Barnet portal knowledge in one file. The alternative split query inventory and qualification logic across extra modules. That split increased reader work without adding another consumer.

The selected design retains one idea from the alternative. Open-case and active-appeal queries remain different typed variants because they prove different coverage promises.

## Tradeoffs accepted

- We accept authority-local IDOX parsing in exchange for independent Barnet semantics.
- We accept one restart for an old unscoped checkpoint in exchange for exact-scope resume safety.
- We accept failure when a configured form option or count changes in exchange for avoiding a false completeness claim.
- We accept a dedicated Barnet command in exchange for avoiding a shared qualification framework with only two examples.

## Alternatives considered

A separate inventory module lost because callers would need to trace query ownership across the inventory, adapter, qualification package, and script. A shared IDOX adapter lost because portal fields, status meanings, and result behavior remain authority-specific. The capped current-list route lost because the official portal already proved that it cannot enumerate the required set.

## Open risks

The official portal may rate-limit the run or cap one advanced partition. Either outcome prevents a receipt. Two weekly refreshes approximately 7 and 14 days after bootstrap cannot happen on the implementation date and remain pending.

## Implementation and live result

The adapter, tests, qualification command, and typed receipt are implemented.
The full deterministic suite reached 100% branch coverage before the live run.

The first live attempt on 16 September 2026 stopped on an official HTTP 429
during detail collection. Durable state contains 10 discovered references, 6
committed applications, one pending retry, and a resumable first-query page
checkpoint. No receipt was written. This is a source-health blocker, not a
successful bootstrap. The two weekly refreshes remain future work and Barnet
must remain discovery-only.
