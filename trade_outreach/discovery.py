from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.parse
import urllib.error
import urllib.request

from .config import Settings


BLOCKED_DOMAINS = {
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "x.com",
    "twitter.com",
    "yelp.com",
    "yellowpages.com",
    "mapquest.com",
    "google.com",
    "bing.com",
    "thomasnet.com",
    "kompass.com",
    "dnb.com",
    "zoominfo.com",
    "apollo.io",
    "valvesoftware.com",
}


def discover_websites(settings: Settings, query: str, count: int = 10) -> list[dict]:
    return google_places_search(settings, query, count)


def google_places_search(settings: Settings, query: str, count: int) -> list[dict]:
    if not settings.google_places_api_key:
        raise RuntimeError("GOOGLE_PLACES_API_KEY is not configured")

    endpoint = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings.google_places_api_key,
        "X-Goog-FieldMask": ",".join(
            [
                "places.displayName",
                "places.websiteUri",
                "places.formattedAddress",
                "places.nationalPhoneNumber",
                "places.internationalPhoneNumber",
                "nextPageToken",
            ]
        ),
    }
    rows: list[dict] = []
    page_token = ""
    remaining = max(count, 1)

    while remaining > 0:
        payload = {
            "textQuery": query,
            "pageSize": min(remaining, 20),
            "languageCode": settings.google_places_language_code,
            "regionCode": settings.google_places_region_code,
        }
        if page_token:
            payload["pageToken"] = page_token
            time.sleep(2)
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        data = fetch_json_with_retries(request, attempts=3, timeout=12)

        for place in data.get("places", []):
            website = place.get("websiteUri") or ""
            if not website:
                continue
            domain = domain_of(website)
            if not domain or is_blocked_domain(domain):
                continue
            display_name = place.get("displayName") or {}
            phone = place.get("internationalPhoneNumber") or place.get("nationalPhoneNumber") or ""
            rows.append(
                {
                    "company_name": display_name.get("text") or "",
                    "website": root_url(website),
                    "source": f"google_places:{query}",
                    "phone": phone,
                    "address": place.get("formattedAddress") or "",
                }
            )

        page_token = data.get("nextPageToken") or ""
        remaining = count - len(rows)
        if not page_token:
            break

    return dedupe_by_website(rows)


def fetch_json_with_retries(request: urllib.request.Request, attempts: int, timeout: float) -> dict:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            message = ""
            try:
                payload = json.loads(body)
                error = payload.get("error") or {}
                message = error.get("message") or body[:500]
            except json.JSONDecodeError:
                message = body[:500]
            raise RuntimeError(f"Google Places HTTP {exc.code}: {message}") from exc
        except (urllib.error.URLError, ssl.SSLError, socket.timeout, TimeoutError) as exc:
            last_error = exc
            if attempt >= attempts:
                break
            time.sleep(2 * attempt)
    raise RuntimeError(f"Google Places request failed after {attempts} attempts: {last_error}")


def dedupe_by_website(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for row in rows:
        website = row.get("website", "").rstrip("/")
        if not website or website in seen:
            continue
        seen.add(website)
        unique.append(row)
    return unique


def domain_of(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def root_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme or "https"
    host = parsed.netloc
    return f"{scheme}://{host}".rstrip("/")


def is_blocked_domain(domain: str) -> bool:
    return any(domain == blocked or domain.endswith("." + blocked) for blocked in BLOCKED_DOMAINS)
