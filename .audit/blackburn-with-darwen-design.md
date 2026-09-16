# Blackburn with Darwen live adapter design

## Caller usage

```python
session = await BlackburnPlaywrightSession.create()
window = DiscoveryWindow(
    start=date(2026, 8, 18),
    end=date(2026, 9, 16),
    include_open=True,
)
report = await collector.collect(
    AuthorityId("blackburn-with-darwen"),
    window,
    session,
)
```

The collector supplies the saved checkpoint on resume. An immediate rerun sees a terminal checkpoint and performs no browser operation.

## Data shape

```python
class BlackburnQueryKind(StrEnum):
    RECEIVED = "received"
    VALID = "valid"
    DECISION = "decision"
    OLDER_OPEN = "older-open"


class BlackburnDateRangeV1(FrozenModel):
    start: date
    end: date


class BlackburnQueryV1(FrozenModel):
    kind: BlackburnQueryKind
    date_range: BlackburnDateRangeV1


class BlackburnCheckpointV2(FrozenModel):
    scope: BlackburnDiscoveryScope
    pending_queries: tuple[BlackburnQueryV1, ...]
    completed_queries: tuple[BlackburnQueryV1, ...]
    seen_references: tuple[str, ...]
    complete: bool
```

`BlackburnSearchRowV1` retains the public reference, the numeric detail locator, the proposal, the address, and the public decision value. `BlackburnApplicationV2` owns the complete labelled detail fields and document metadata. Document rows retain the download URL as metadata. No code follows that URL.

## Module ownership

`adapter.py` owns query inventory, checkpoint transitions, parsing, completeness, and normalisation. `page_object.py` owns the browser form interaction and rendered evidence. `qualify_blackburn_with_darwen.py` owns the persisted live run and its receipt. General browser transport continues to own image and media blocking.

## Synthesis decision

Use the current official Citizen portal through the existing guarded Playwright boundary. Do not reuse Cheshire East types even though both portals use similar forms. Build an ordered authority-local query inventory. Split a range whenever the portal returns 30 rows. Fail when a one-day range returns 30 because the portal exposes no pager or total.

For `include_open`, scan received dates from 1 January 1977 through the day before the requested 30-day window. Keep rows with an empty public decision. The council page documents 1977 as the lower bound. This route is expensive, but it is the only visible official route that can be complete without guessing a private endpoint.

The implementation must not promote readiness unless the entire historical scan, every detail, all document metadata, durable persistence, and the zero-network rerun pass. Two later weekly cycles remain pending in the acceptance ledger.
