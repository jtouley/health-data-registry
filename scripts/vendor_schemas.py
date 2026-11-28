"""Fetch Bitol's official ODPS and ODCS JSON schemas into src/health_data_registry/schemas/ (development only).

The server never fetches a schema: `validate` reads the vendored copy and refuses it unless its sha256 is the one
recorded in product.py (ODPS, ODCS). Run this only to restore a copy, or after changing the recorded URL and hash on
purpose for a new version:

    uv run python scripts/vendor_schemas.py           # fetch, check the hash, write
    uv run python scripts/vendor_schemas.py --check   # only check the vendored copies
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import sys
import urllib.parse

from health_data_registry.product import VENDORED


def _get(url: str) -> bytes:
    """The bytes at an HTTPS URL; HTTPSConnection can't speak any other scheme, and a redirect is refused."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise SystemExit(f"refusing a non-HTTPS URL: {url}")
    conn = http.client.HTTPSConnection(parts.hostname, timeout=60)
    try:
        conn.request("GET", parts.path, headers={"User-Agent": "health-data-registry-vendor-schemas"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise SystemExit(f"{url}: HTTP {resp.status}")
        return resp.read()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--check", action="store_true", help="only check the vendored copies' sha256")
    args = ap.parse_args(argv)
    failed = 0
    for spec in VENDORED:
        local = spec.path.read_bytes() if spec.path.is_file() else b""
        data = local if args.check else _get(spec.url)
        digest = hashlib.sha256(data).hexdigest()
        if digest != spec.sha256:
            print(f"{spec.standard} {spec.api_version}: sha256 {digest} is not the recorded {spec.sha256}")
            failed += 1
            continue
        if not args.check:
            spec.path.write_bytes(data)
        print(f"ok {spec.standard} {spec.api_version}: {spec.path.name} ({spec.url})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
