# Leeds portal walkthrough and live qualification

Initial walkthrough: 15 September 2026. Live qualification evidence: 16
September 2026.

## Source and register boundary

- Portal: `https://publicaccess.leeds.gov.uk/online-applications/`
- Shape: IDOX Public Access
- Covered register: planning applications and associated appeals, conditions,
  consultations, relationships, and published document metadata
- Excluded registers: building control and licensing

The portal uses a server-side session. Every live request is serialized per
host with a minimum two-second gap. Portal locators (`keyVal`) are source-local
routing data; the published planning reference remains the human alias.

## Exhaustive bootstrap discovery

The initial 30-day window is 18 August through 16 September 2026 and includes
older open applications. One typed, ordered inventory reconciles 43 searches:

1. validated and decided weekly lists for each of five intersecting weeks;
2. advanced validated-date and decision-date searches bounded to the window;
3. `Current` searches partitioned across the 30 observed case types; and
4. one `Appeal lodged` search for active appeals whose application status is
   not necessarily current.

The unpartitioned current search exceeds the portal result cap. A capped query
is a qualification failure, never an empty result. The adapter therefore
requires the exact observed case-type taxonomy, exhausts every page, reconciles
displayed totals, binds every response to its requested page, and fails closed
if the form, taxonomy, or pagination identity drifts. Its exact named-control
inventory and cardinality allow only the two captured repeated opaque fields,
so an unknown filter or duplicate discriminator cannot silently narrow the
search. A repeated first page cannot advance a resumed checkpoint.

The clean live run completed all ten weekly partitions with totals
`116, 162, 154, 140, 102, 101, 156, 140, 0, 93`. It then reached page 10 and
row 90 of the first advanced validated-date partition. At that checkpoint,
1,148 unique references exactly matched 1,148 retained applications, with zero
failed current sections and zero pending retries.

The first page-10 attempt returned an unparseable portal response. Four bounded
resume commands then each exhausted three no-progress sessions with
`SourceUnavailableError`. The checkpoint remains resumable at
`advanced|validated|2026-08-18|2026-09-16`, page 10, row 90. Because only 10 of
43 queries are complete, no qualification receipt exists and the live
bootstrap remains blocked.

## Detail and section contracts

Successful live records established the summary and document-index contracts.
`Reference`, `Proposal`, and `Status` are required. Address, application type,
validated date, appeal status, and appeal decision remain optional native
values because older records can leave them blank. The summary reference must
equal the queued reference.

Document metadata is retained without opening attachment bodies. Two exact
table shapes were observed:

- six cells: selection, date published, document type, measure, description,
  and view; and
- four cells: date published, document type, description, and view.

The selection cell may contain the portal's accessibility label and checkbox.
Unknown headers, row widths, dates, links, or pagination remain failed sections.
The parsed metadata rows must exactly match the displayed `Documents (N)`
count, so a truncated or header-only nonzero index cannot pass as complete.
The exact Leeds permission-denied page maps to unavailable documents rather
than empty documents. A header-only table or the explicit no-documents wording
maps to empty.

The portal intermittently returns an HTTP-200 remote-exception shell for
summary or documents. The adapter retries that exact response three times. A
persistent shell remains a retryable whole-record failure and never replaces a
previous section with empty data. Document transport failures also remain
whole-record retryable; they are not converted into a successful snapshot with
a failed section. Leeds states that public comment text is not published, so
comments are represented as unavailable and attachment bodies are not used as
a substitute.

## Qualification and readiness

The isolated live command is:

```sh
uv run python scripts/qualify_leeds.py \
  --confirm-live \
  --data-dir .yimby/qualification-leeds-2026-09-16 \
  --start 2026-08-18 \
  --end 2026-09-16 \
  --include-open
```

Add `--resume` when continuing the recorded checkpoint. A successful command
must complete all 43 searches, prove exact checkpoint/queue/application-set
agreement, verify retained evidence and SQLite integrity, leave no failed or
pending work, make no attachment-body request, and perform an immediate
zero-network rerun. Only then does it atomically write
`leeds-qualification-v1.json`.

Even that receipt proves only the bootstrap. Successful live refreshes around
23 and 30 September 2026 are still required for operational qualification.
Leeds therefore remains below `live-ready`.
