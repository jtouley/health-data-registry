# CDC WONDER

API documentation: <https://wonder.cdc.gov/wonder/help/wonder-api.html>

Tested on 24 Nov 2026 against Underlying Cause of Death 1999–2020 (`D76`), two requests 2 minutes apart:

| Query | Result |
|---|---|
| National deaths by year | HTTP 200, 22 rows (e.g. 1999: 2,391,399 deaths, crude rate 857.0, 95% CI 855.9–858.1) |
| Deaths by county (`B_1 = D76.V9-level2`) | Refused: "Only national data are available for this dataset when using the WONDER web service. Please check that your query does not group results by region, division, state, county or urbanization." |

- **Request:** `POST https://wonder.cdc.gov/controller/datarequest/{DBID}` with a `request_xml` form field and
  `accept_datause_restrictions=true`. The parameter set must match CDC's published example. An empty selection
  is rejected ("No values were selected"), and so is a value of `""` next to `*All*`.
- **Pace:** CDC asks for one query at a time, about every 2 minutes. The server needs a queue and a cache.
- **What travels with every result:** "powered by CDC WONDER", the database's citation, its footnotes, and its
  suppression. Counts of 1–9 are suppressed, and the unreliable-rate rule differs by database.
- **Sub-national data:** the web form offers state and county; the API refuses them (above). The form hasn't
  been tried from a program yet. The project's rule: a source we aren't blocked from in plain terms, we use.
- **robots.txt** names AI agents, including `Claude-User`, with no Disallow rule for them; its only directive is
  no AI training.
