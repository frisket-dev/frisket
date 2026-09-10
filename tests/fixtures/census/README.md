# Census replay fixtures

These files are replay fixtures for Census integration tests. Tests should load
them from disk instead of inventing inline HTTP payloads.

- `coordinatesbatch_current_current.csv` was captured from the live Census
  coordinate batch GeoLookup endpoint on 2026-06-13 with:
  `benchmark=Public_AR_Current`, `vintage=Current_Current`, and uploaded CSV
  `1,-77.0365,38.8977`.
- `coordinatesbatch_acs2024_current.csv` and `coordinatesbatch_dedupe.csv`
  were captured from the same endpoint with `vintage=ACS2024_Current`; that
  vintage intentionally has blank block fields.
- `acs_2024_dc_tracts.json` is the direct ACS API response shape consumed by
  `census_demographics`, captured from the live Census ACS API on 2026-06-13
  with the configured `CENSUS_API_KEY` for all 2024 ACS5 District of Columbia
  tracts. The runner test selects the two tracts returned by
  `coordinatesbatch_dedupe.csv`.
- `acs_2024_dc_block_groups_980000.json` is the direct Census ACS API response
  for block groups under District of Columbia tract `980000`, matching the
  block-returning GeoLookup fixture.

Current secret note: `.secrets/frisket.env` and prod both have an active
`CENSUS_API_KEY`. The prod app container was verified on 2026-06-13 with a
direct ACS API request. Refresh `acs_2024_dc_tracts.json` directly from Census
with the command below when the ACS vintage or variable pack changes.

Refresh command template:

```sh
curl -sS "https://api.census.gov/data/2024/acs/acs5?get=NAME,B01003_001E,B01002_001E,B19013_001E,B17001_001E,B17001_002E,B15003_001E,B15003_022E,B15003_023E,B15003_024E,B15003_025E,B03002_001E,B03002_003E,B03002_004E,B03002_006E,B03002_012E,B25003_001E,B25003_002E&for=tract:*&in=state:11%20county:001&key=${CENSUS_API_KEY}" \
  > tests/fixtures/census/acs_2024_dc_tracts.json
```
