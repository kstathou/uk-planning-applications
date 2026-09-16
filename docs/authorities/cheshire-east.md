# Cheshire East portal walkthrough

Observed on 15 September 2026.

## Source

- Portal: `https://pa.cheshireeast.gov.uk/planning/index.html?fa=search`
- Shape: custom council register
- Separate route: `?fa=search_map`

The older guessed Idox route is not a source. It returned 404 during the portal census.

## Search

The search form is named `form`, uses `POST`, and submits to
`/planning/index.html`. It exposed these controls:

- `application_reference_number`, `application_type_id`, `proposal`, and
  `decision_type_id`
- `Applicant[applicant_name]`, `Applicant[company_name]`,
  `Agent[agent_name]`, and `Agent[company_name]`
- `ps_development_code_id`, `SiteAddress[magic]`, `SiteAddress[postcode]`,
  `SiteAddress[Street][street_description]`, and `site_address_description`
- `site_address_x`, `site_address_y`, `ward_id`, and `community_id`
- valid, received, proposed committee, and decision-issued date pairs
- hidden `fa` and `submitted` values

It stated that appeals are not visible and directed users to the Planning
Inspectorate.

A valid-date-from search for 14 September 2026 initially returned a long result
table in the same document. Each visible result exposed reference, application
type, location, proposal, optional consultation-close date, and a `View`
control. The displayed records included application references such as
`26/3180/DSC`, `26/3335/PRIOR-1A`, and `26/3322/NMA`.

On 16 September, a same-day valid-date-from submission unexpectedly returned
four old references rather than a trustworthy same-day set. The source did not
show a result total or pagination control in that response. This observation
prevents the implementation from treating the form as an exact arbitrary date
window.

The visible `View` control for `26/3322/NMA` accepted focus but did not produce readable detail content during this bounded walkthrough. Detail extraction is therefore a source failure or unresolved client interaction, not an empty record.

## Collection consequences

- Implement the custom form field names and retain every native classification value.
- Run valid, received, and decision windows independently. Proposed committee dates are an additional change signal, not a replacement for application dates.
- Convert supplied British National Grid coordinates to WGS84 while retaining the native X and Y values.
- Keep appeals explicitly unavailable from this register and link to the separate authoritative source when appeal collection is added.
- Treat the same-document result table as a bounded enumeration whose total and pagination rules still need inspection.
- Do not claim application, document, or comment completeness until a detail route succeeds.

## Verification status

`VERIFIED` for the form contract, `application_results_table`, and numeric
locator in each `.view_application[data-id]` control. The HTTP adapter submits
the recorded valid-date-from form, persists every visible reference and numeric
locator in its checkpointed batch, then stops explicitly because the response
does not prove a total or pagination boundary.

`INCONCLUSIVE` for exact-window fidelity, result count, pagination, detail
retrieval, documents, comments, and incremental refresh. The adapter is
discovery-only and deliberately never labels the observed table complete.
