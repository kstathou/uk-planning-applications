# Devon County Council portal walkthrough

Walkthrough and qualification date: 16 September 2026.

## Source and scope

- Planning and appeal register: `https://planning.devon.gov.uk/`
- Covered records: minerals, waste, county council development, and associated
  appeals.
- Qualification window: 18 August through 16 September 2026, inclusive.
- Older-open policy: both planning applications and appeals with
  `Outstanding=true`.

The official register presents a copyright and data-use disclaimer before a
protected route when the session has not accepted it. The acceptance form posts
to `/Disclaimer/Accept`; a disclaimer is never interpreted as an empty search or
an application record.

## Exact discovery contract

The adapter fetches `/Search/Advanced`, requires one POST form with action
`/Search/Results`, and preserves successful controls in DOM order. It switches
the source-owned `SearchPlanning` and `SearchAppeals` controls for the relevant
query while leaving enforcement disabled.

The ordered qualification inventory is exactly:

1. `received:2026-08-18:2026-09-16` — 3 rows on one page.
2. `determined:2026-08-18:2026-09-16` — 1 row on one page, returned directly
   as the detail for `PRE/1820/2026`.
3. `outstanding:planning:true` — 55 rows on six pages of
   `10, 10, 10, 10, 10, 5`.
4. `appeal-received:2026-08-18:2026-09-16` — 0 rows on one terminal page.
5. `appeal-determined:2026-08-18:2026-09-16` — 0 rows on one terminal page.
6. `outstanding:appeals:true` — 11 rows on two pages of `10, 1`.

Planning results link to `/Planning/Display/...`; appeal results link to the
distinct `/Appeals/Display/...` contract. Both origins and exact path families
are validated before every request and redirect.

Result pages do not publish a total or displayed row range. Completeness is
therefore proved only from observable pager facts: one current-page marker, the
complete consecutive numbered-link inventory, exact portal-provided locators,
ten rows on every page with a forward link, and no forward link on the terminal
page. The adapter never constructs a pagination URL. Malformed, shifted,
truncated, duplicated, or mixed result shapes fail closed.

The persisted checkpoint owns the exact scope, completed-query prefix, durable
row and page totals, active-page replay proofs, and every unique reference with
its source locator. Partial scope changes are rejected. A coherent terminal
checkpoint may start the next weekly scope, while an exact terminal rerun
returns before opening a network route.

## Native records and documents

Planning details retain application and decision fields, consultation expiry,
committee and issue dates, applicant and agent addresses, local members, BNG
coordinates where published, constraints, and planning-consultee rows. Appeal
details retain the related planning and enforcement references, UPRN, site,
appeal type and method, appellant and agent fields, all published appeal
milestones, officers, PINS reference, parish and ward, decision and abeyance
fields, costs fields, coordinates where published, and appeal-consultee rows.
The source's malformed but non-empty consultee rows are retained without
guessing at missing columns.

The live store contains 56 planning records and 11 appeal records. It has 30
published BNG coordinate pairs (24 planning and 6 appeal), 16 records with
published constraints, and 21 with published consultations (16 planning and 5
appeal).

Document metadata is read only from the returned `PlanningdocTable` and
`document-list` HTML. The parser validates the decorated header, category
groups, three-cell rows, one exact official download link per row, and Created
date. It retains module, record number, plan and image identifiers, plan flag,
filename, category, and published date without opening an attachment. The
qualification retained 1,470 current rows across 28 records: 1,368 planning
rows across 25 records and 102 appeal rows across 3 records. The other 39
records explicitly report documents unavailable. All comments remain
unavailable because the register exposes responses as document attachments.

## Live qualification receipt

The durable receipt is
`.yimby/qualification-devon-2026-09-16/devon-qualification-v3.json` with SHA-256
`0db784ad49bebd39d6094f095898c5e0c7c966f66bb30b514440cf8a7451270c`.
It records:

- all six completed query keys with the row and page totals above;
- 67 unique references, applications, native versions, observations, evidence
  registrations, and compressed evidence files;
- 67 application versions, 28 complete document-section versions, 39
  explicitly unavailable document sections, and zero comment versions;
- zero pending retries, failed current sections, unmapped records, and
  attachment body requests;
- SQLite integrity, exact durable reference/application agreement, complete
  per-observation evidence reconciliation, and a canonical all-file inventory;
- 86 official requests and 8,234,248 transferred bytes on the first pass;
- an immediate terminal rerun with 0 requests, 0 bytes, and 0 attachment
  bodies; and
- byte-for-byte receipt preservation under a separate `--resume` command.

All twelve named receipt checks pass. Weekly cycles due 23 and 30 September
2026 remain truthfully `pending`. The adapter and bootstrap are live collection
verified for this scope, but operational qualification and `LIVE_READY`
promotion remain prohibited until those genuinely later cycles succeed.
