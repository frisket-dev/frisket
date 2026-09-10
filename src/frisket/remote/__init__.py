"""Client side of remote team-server administration.

`frisket remote` / `frisket users` / `frisket token` / `frisket secrets` /
`frisket proxy` verbs, a named-server profile store
(`~/.config/frisket/config.toml`), and a
thin HTTP client for the operator-token admin API served by
`frisket.team.admin_routes`. Nothing here imports server composition code:
this package runs on a workstation that only has the URL and a token.
"""
