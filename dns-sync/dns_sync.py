#!/usr/bin/env python3
"""Sync per-handle `_atproto.<handle>.<parent>` TXT records to Cloudflare DNS
so ATProto handle resolution works for every user on the PDS.

Polls the Tranquil `users` table every POLL_SECONDS. For each active user with
a `did:plc:` identity, ensures a TXT record exists at
`_atproto.<handle>.<PDS_PARENT_DOMAIN>` with content `did=<did>`. Stale records
(handles that were renamed or accounts that were deactivated) are removed.

The script is idempotent: re-runs converge on the desired state. Cloudflare's
own rate limits (~1200 req/5min for free tier) are not a concern at PDS scale.

Required env vars:
    DATABASE_URL        Postgres connection string (Tranquil's database).
    CF_API_TOKEN        Cloudflare API token with Zone:DNS:Edit on PDS_PARENT_DOMAIN.
    CF_ZONE_ID          Cloudflare zone ID for the parent domain.
    PDS_PARENT_DOMAIN   The full parent domain users live under, e.g. pds.halvacoffee.fyi.
"""

import logging
import os
import sys
import time
from typing import Any

import psycopg
import requests

POLL_SECONDS = 30
CF_API_BASE = "https://api.cloudflare.com/client/v4"
HTTP_TIMEOUT = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("dns-sync")


def required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        log.error("missing required env var %s", name)
        sys.exit(1)
    return val


DB_URL = required("DATABASE_URL")
CF_TOKEN = required("CF_API_TOKEN")
ZONE_ID = required("CF_ZONE_ID")
PARENT = required("PDS_PARENT_DOMAIN").rstrip(".")


def cf_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {CF_TOKEN}",
        "Content-Type": "application/json",
    }


def cf_list_txt() -> list[dict[str, Any]]:
    """Fetch all TXT records in the zone. Paginates via result_info cursor."""
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        r = requests.get(
            f"{CF_API_BASE}/zones/{ZONE_ID}/dns_records",
            headers=cf_headers(),
            params={"type": "TXT", "per_page": 100, "page": page},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        body = r.json()
        if not body.get("success"):
            raise RuntimeError(f"cloudflare list failed: {body}")
        out.extend(body["result"])
        total_pages = body["result_info"]["total_pages"]
        if page >= total_pages:
            break
        page += 1
    return out


def cf_create_txt(name: str, content: str) -> None:
    r = requests.post(
        f"{CF_API_BASE}/zones/{ZONE_ID}/dns_records",
        headers=cf_headers(),
        json={"type": "TXT", "name": name, "content": content, "ttl": 300},
        timeout=HTTP_TIMEOUT,
    )
    body = r.json()
    if not r.ok or not body.get("success"):
        raise RuntimeError(f"create {name} failed: {body}")
    log.info("created TXT %s -> %s", name, content)


def cf_update_txt(record_id: str, name: str, content: str) -> None:
    r = requests.put(
        f"{CF_API_BASE}/zones/{ZONE_ID}/dns_records/{record_id}",
        headers=cf_headers(),
        json={"type": "TXT", "name": name, "content": content, "ttl": 300},
        timeout=HTTP_TIMEOUT,
    )
    body = r.json()
    if not r.ok or not body.get("success"):
        raise RuntimeError(f"update {name} failed: {body}")
    log.info("updated TXT %s -> %s", name, content)


def cf_delete_txt(record_id: str, name: str) -> None:
    r = requests.delete(
        f"{CF_API_BASE}/zones/{ZONE_ID}/dns_records/{record_id}",
        headers=cf_headers(),
        timeout=HTTP_TIMEOUT,
    )
    body = r.json()
    if not r.ok or not body.get("success"):
        raise RuntimeError(f"delete {name} failed: {body}")
    log.info("deleted stale TXT %s", name)


def fetch_active_users() -> dict[str, str]:
    """Return {handle: did} for every active user with a PLC identity."""
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT handle, did
                FROM users
                WHERE deactivated_at IS NULL
                  AND did LIKE 'did:plc:%'
                """
            )
            return {h: d for h, d in cur.fetchall()}


def reconcile() -> None:
    users = fetch_active_users()
    log.info("found %d active plc user(s)", len(users))

    existing = {
        r["name"]: r
        for r in cf_list_txt()
        if r["name"].startswith("_atproto.")
    }
    log.info("found %d existing _atproto TXT record(s)", len(existing))

    desired: dict[str, str] = {}
    for handle, did in users.items():
        # Handles are FQDNs already (e.g. "maid.pds.halvacoffee.fyi"), so the
        # _atproto subdomain is "_atproto.<handle>" with no extra suffix.
        desired[f"_atproto.{handle}"] = f"did={did}"

    # Add or update desired records.
    for name, content in desired.items():
        rec = existing.get(name)
        if rec is None:
            cf_create_txt(name, content)
        elif rec["content"] != content:
            cf_update_txt(rec["id"], name, content)

    # Remove _atproto.* records that aren't desired.
    # Two cases: (a) handle renamed, (b) account deactivated, (c) zone-wide
    # records we don't own (parent domain's _atproto). Skip parent-level
    # records (anything at exactly _atproto.<PARENT>) so we don't delete the
    # apex TXT or the wildcard CNAME catch-all.
    wanted_names = set(desired)
    for name, rec in existing.items():
        if name in wanted_names:
            continue
        if name == f"_atproto.{PARENT}":
            continue
        # One-time migration: v1 of this script created bugged records like
        # `_atproto.<handle>.<PARENT>` (double-suffixed). Once the canonical
        # record exists, delete the bugged one. Detect by the trailing
        # `.PARENT` suffix on an _atproto record.
        if name.endswith(f".{PARENT}") and name.startswith(f"_atproto."):
            short = name[: -len(f".{PARENT}")]
            if short in wanted_names:
                log.warning(
                    "removing bugged record %s (canonical %s exists)",
                    name,
                    short,
                )
                cf_delete_txt(rec["id"], name)
                continue
        cf_delete_txt(rec["id"], name)


def main() -> None:
    log.info("dns-sync starting (parent=%s)", PARENT)
    while True:
        try:
            reconcile()
        except Exception:
            log.exception("reconcile failed; will retry")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
