"""frisket.x reference scraper for plugin-contributed URL matchers.

The end-to-end plugin the spec names: it declares the x.post / x.profile matchers
(in plugin.json) and the x.scrape_post handler action here. Classification runs
host-side; THIS handler runs in the plugin subprocess under the declared
capability. It does no network — the reference records enough evidence (the
plugin identity + the subprocess pid + the parsed status id) for the contract
test to prove the in-subprocess execution posture without a live x.com call.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from frisket.plugins.sdk import Plugin

plugin = Plugin()

_STATUS_RE = re.compile(r"^/(?P<handle>[A-Za-z0-9_]{1,15})/status/(?P<status_id>\d+)$")


@plugin.op(
    kind="x.scrape_post",
    handler_key="scrape_post",
    title="Scrape X post",
    description="Scrape a single x.com/twitter.com post into a json cell.",
    scope="row",
    inputs=[{"name": "url", "column_types": ["text", "link"]}],
    writes=[{"name": "post", "type": "json"}],
)
def scrape_post(ctx, row, params, url):
    del row, params
    raw = "" if url is None else str(url).strip()
    parsed = urlparse(raw)
    match = _STATUS_RE.match(parsed.path or "")
    if match is None:
        raise ValueError(f"not an x.com status URL: {raw!r}")
    return {
        "post": {
            "provider": "x",
            "url": raw,
            "handle": match.group("handle"),
            "status_id": match.group("status_id"),
            # Evidence the contract test checks: the handler ran in the plugin
            # subprocess (distinct pid) under the plugin's identity.
            "scraped_by_plugin_id": ctx.plugin_id,
            "scraped_by_handler_key": ctx.handler_key,
            "scraper_pid": os.getpid(),
        }
    }
