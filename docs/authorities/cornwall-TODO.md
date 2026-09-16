# Cornwall qualification TODO

Cornwall is not implemented or live-qualified. The official planning portal
closed the connection without returning an HTTP response during discovery.

Before writing or resuming a scraper:

- Recheck the authority's official publications for a supported API or data
  dump and use that in preference to HTML scraping.
- If no structured source exists, repeat the browser walkthrough and record the
  exact search, detail, pagination, comments, and document-metadata routes.
- Implement the authority-specific native schema and collector with resumable
  checkpoints, request-bound evidence, and fail-closed partial-section state.
- Add a Cornwall live qualification command. The existing
  `scripts/smoke_cornwall.py` is not a substitute for bootstrap and refresh
  qualification.
- Complete a fresh live bootstrap and two real weekly refresh cycles, run the
  full repository gates, and obtain a no-comments review of the exact final
  commit.

No live-readiness claim or qualification receipt exists for this checkpoint.
