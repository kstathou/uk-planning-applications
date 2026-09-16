# Camden qualification TODO

This branch is a useful Camden Socrata checkpoint, but it is not ready to be
described as operationally qualified. Complete these items before merge:

- Add an upgrade path, or fail closed, when a retained store contains the old
  `camden-jsf-search` source identity. Cover application and dependent-row
  migration so a new Socrata discovery cannot duplicate the retained corpus.
- Make bulk Socrata discovery satisfy due refresh schedules before per-record
  refresh work. Test a genuinely due weekly cycle so a follow-up does not issue
  one request for every retained application before reading the feed.
- Rehydrate and parse every registered discovery capture during qualification.
  Reconcile request identity, body contents, pagination cursor continuity, and
  checkpoint contents instead of validating only that a digest exists.
- Preserve bootstrap and later-cycle receipts as append-only, scope-specific
  proof. Link the two real weekly follow-ups rather than overwriting the prior
  receipt or reporting `weekly_cycles` as permanently pending.
- Repeat the full deterministic gates, run a fresh live bootstrap plus two real
  weekly follow-ups, and obtain a no-comments review of the exact final commit.

The current PR must remain marked incomplete until these items are closed.
