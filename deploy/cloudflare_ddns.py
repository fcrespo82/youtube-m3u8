#!/usr/bin/env python3
"""Keep a DNS-only Cloudflare A record aligned with the public IPv4 address."""

import json
import os
import urllib.parse
import urllib.request


def request(url, token, method="GET", body=None):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=20) as response:
        data = json.loads(response.read())
    if not data.get("success"):
        raise RuntimeError(data.get("errors", "Cloudflare API request failed"))
    return data["result"]


def main():
    token = os.environ["CLOUDFLARE_API_TOKEN"]
    zone = os.environ["CLOUDFLARE_ZONE_ID"]
    host = os.environ["PUBLIC_HOST"]
    with urllib.request.urlopen("https://api.ipify.org", timeout=20) as response:
        address = response.read().decode().strip()
    base = f"https://api.cloudflare.com/client/v4/zones/{zone}/dns_records"
    records = request(base + "?" + urllib.parse.urlencode({"type": "A", "name": host}), token)
    if not records:
        raise RuntimeError(f"No A record found for {host}")
    record = records[0]
    if record["content"] == address and record["proxied"] is False:
        return
    body = json.dumps({"type": "A", "name": host, "content": address, "ttl": 120, "proxied": False}).encode()
    request(f"{base}/{record['id']}", token, "PUT", body)


if __name__ == "__main__":
    main()
