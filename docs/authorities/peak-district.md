# Peak District portal walkthrough

Walkthrough and live qualification date: 16 September 2026.

## Sources

- Current AssureLive portal: `https://planning.peakdistrict.gov.uk/AssureLive/`
- Legacy information portal: `https://portal.peakdistrict.gov.uk/`

AssureLive is the authoritative current search, detail, and document-metadata
surface. The adapter retains the established `peak-district-legacy` source
identity for public references so an upgraded locator does not create duplicate
durable applications. The manifest records AssureLive separately as the current
source.

## Discovery contract

The live adapter submits five ordered AssureLive queries for one exact scope:

1. Received between the inclusive start and end dates.
2. Validated between the same dates.
3. Decided between the same dates.
4. Any-time status `REGISTERED`.
5. Any-time status `APPEAL LODGED`.

The first three queries prove bounded 30-day discovery. The last two reconcile
older open applications and active appeals. References found by more than one
query are de-duplicated through the checkpoint's ordered seen-reference set.

The search shell declares the advanced partial at
`OnlinePlanningAdvanceSearchView?SearchFor=0`. A bounded query sends the
selected `AdvanceSearch.<Date>Between` radio, its two dates, and omits the
corresponding any-time radio. An open query sends the selected application
status and preserves the portal's any-time date controls. Pagination submits
the selected query again, the result-page controls, and the portal's serialized
search state.

Result pages reconcile the readable `Total record(s)` value with
`TotalRecords`, `PageCount`, `PageSize`, and the current page. AssureLive shows
only a window of page links for large result sets, so the adapter requires the
current and adjacent links to be present and every visible page to be in range.
It then requests all pages sequentially from the reconciled page count. The
readable application number must agree with the `applicationNumber` in every
opaque overview locator.

## Detail and document metadata

The overview parser retains the public reference, type, proposal, status,
address, parish, registered date, applicant, agent, and planning officer. It
fails closed if the page reference differs from the discovery reference.

The document endpoint is accepted only when its declared route matches
`GetOnlineDocuments`. Every document-list page reconciles three reported
counts, its current page, its page size, its expected row count, and its
windowed paginator. Each row retains only the published date, title, type, and
attachment URL. The collector never opens an attachment body.

The checked records exposed no public comments tab. Their comment section is
therefore unavailable, not empty. If a comments tab appears before its contract
is implemented, the adapter marks that section failed rather than inferring
completeness.

## Live qualification

The accepted local evidence directory is
`.yimby/qualification-peak-district-2026-09-16`. Its typed
`peak-district-qualification-v1.json` receipt records:

- the inclusive scope from 18 August through 16 September 2026;
- the exact five-query inventory and a one-attempt transport policy;
- 377 discovered references and 377 applications;
- 389 native versions, 377 application versions, and 377 document versions;
- zero pending retries, failed sections, unmapped records, and attachment-body
  requests;
- agreement among checkpoint, discovery queue, and retained applications;
- database, evidence-path, and SHA-256 evidence integrity;
- an unchanged immediate rerun with zero requests and zero transferred bytes.

During qualification, stored official evidence exposed windowed pagination on
large search and document sets. Twelve failed current document sections were
scheduled once for an explicit corrective refresh after the parser fix. Eleven
had windowed document pagers and one later document page had been unavailable
on its original single attempt. The accepted run used one attempt per request,
left the retry queue empty, and replaced all twelve with complete current
observations.

## Acceptance boundary

The complete persisted live bootstrap is verified. This does not make the
authority operationally qualified or `LIVE_READY`. Genuinely later weekly
cycles remain pending for 23 September and 30 September 2026. Same-day reruns
do not count toward those cycles.
