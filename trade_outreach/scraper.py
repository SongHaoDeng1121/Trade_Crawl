from __future__ import annotations

import html
import gzip
import re
import socket
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser


EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
MAILTO_HREF_RE = re.compile(r'href=["\']\s*mailto:([^"\'>\s?]+)', re.IGNORECASE)
OBFUSCATED_EMAIL_RE = re.compile(
    r"([A-Z0-9._%+-]+)\s*(?:\\\[at\\\]|\[at\]|\(at\)| at | ä?t | chiocciola | arroba )\s*([A-Z0-9.-]+)\s*(?:\\\[dot\\\]|\[dot\]|\(dot\)| dot | punto | punkt )\s*([A-Z]{2,})",
    re.IGNORECASE,
)
OBFUSCATED_AT_RE = re.compile(
    r"\b([A-Z0-9._%+-]+)\s*(?:\\\[at\\\]|\[at\]|\(at\)|\{at\}| at | ä?t | chiocciola | arroba )\s*([A-Z0-9.-]+\.[A-Z]{2,})\b",
    re.IGNORECASE,
)
CF_EMAIL_RE = re.compile(r'data-cfemail=["\']([0-9a-fA-F]+)["\']')
PHONE_RE = re.compile(r"(?:\+\s?\d{1,3}[\s().-]*)?(?:\(?\d{2,4}\)?[\s.-]+){2,5}\d{2,4}")
JS_ASSIGNMENT_RE = re.compile(
    r"(?:var\s+)?([A-Za-z_$][\w$]*)\s*=\s*((?:\1\s*\+\s*)?(?:'[^']*'|\"[^\"]*\")(?:\s*\+\s*(?:'[^']*'|\"[^\"]*\"))*)\s*;",
    re.IGNORECASE,
)
JS_STRING_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")
BAD_EMAIL_PARTS = {"example", "sentry", "wixpress", "wordpress", "schema.org", "google.com"}
CONTACT_PATHS = [
    "",
    "/contact",
    "/contact-us",
    "/contact_us",
    "/contactus",
    "/kontakt",
    "/kontakt/",
    "/de/kontakt",
    "/de/kontakt/",
    "/kontakt.html",
    "/kontakt.php",
    "/impressum",
    "/impressum/",
    "/de/impressum",
    "/de/impressum/",
    "/impressum.html",
    "/about",
    "/about-us",
    "/company",
    "/unternehmen",
    "/unternehmen/",
    "/ueber-uns",
    "/ueber-uns/",
    "/uber-uns",
    "/products",
    "/produkte",
    "/lagerprogramm",
    "/ansprechpartner",
    "/ansprechpartner/",
    "/de/ansprechpartner",
    "/team",
    "/team/",
    "/de/team",
    "/locations",
    "/standorte",
    "/niederlassungen",
    "/filialen",
    "/people",
    "/contacts",
]
CONTACT_LINK_HINTS = (
    "contact",
    "kontakt",
    "impressum",
    "imprint",
    "ansprechpartner",
    "team",
    "office",
    "vertrieb",
    "verkauf",
    "unternehmen",
    "ueber",
    "uber",
    "about",
    "company",
    "firma",
    "standort",
    "standorte",
    "location",
    "locations",
    "niederlassung",
    "niederlassungen",
    "filiale",
    "filialen",
    "people",
    "personen",
    "mitarbeiter",
    "contactpersonen",
    "sales",
    "inquiry",
    "enquiry",
    "support",
    "service",
)
HIGH_VALUE_LINK_HINTS = (
    "contact",
    "kontakt",
    "ansprechpartner",
    "contacts",
    "contactpersonen",
    "sales",
    "vertrieb",
    "verkauf",
    "inquiry",
    "enquiry",
)
MEDIUM_VALUE_LINK_HINTS = (
    "impressum",
    "imprint",
    "team",
    "people",
    "personen",
    "mitarbeiter",
    "standort",
    "location",
    "niederlassung",
    "filiale",
    "office",
    "unternehmen",
    "ueber",
    "about",
    "company",
    "firma",
    "service",
)
LOW_VALUE_LINK_HINTS = (
    "career",
    "karriere",
    "jobs",
    "privacy",
    "datenschutz",
    "terms",
    "agb",
    "news",
    "blog",
    "press",
    "presse",
    "download",
    "media",
)
SKIP_EXTERNAL_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "google.com",
    "googleapis.com",
    "gstatic.com",
    "schema.org",
}
DEFAULT_FETCH_TIMEOUT = 5
SITEMAP_FETCH_TIMEOUT = 2
MAX_FETCH_ATTEMPTS = 32
MAX_PAGES = 8


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = True
        for key, value in attrs:
            lowered_key = key.lower()
            if lowered_key == "href" and value:
                self.links.append(value)
            if value and (
                lowered_key in {"href", "title", "alt", "aria-label", "content", "value"}
                or lowered_key.startswith("data-")
            ):
                clean = " ".join(value.split())
                if clean:
                    self.text_parts.append(clean)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            clean = " ".join(data.split())
            if clean:
                self.text_parts.append(clean)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")


def fetch_url(url: str, timeout: float = DEFAULT_FETCH_TIMEOUT) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "identity",
            "Accept-Language": "en-US,en;q=0.9,it;q=0.8,de;q=0.8,fr;q=0.7",
            "Cache-Control": "no-cache",
            "Cookie": "cookieyes-consent=consentid:yes,action:yes,necessary:yes,functional:yes,analytics:yes,advertisement:yes; cookie_notice_accepted=true; viewed_cookie_policy=yes",
        },
    )
    try:
        return fetch_with_urllib(request, timeout)
    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError):
        return fetch_with_curl(url, timeout)


def fetch_with_urllib(request: urllib.request.Request, timeout: float) -> str:
    contexts = [ssl.create_default_context(), ssl._create_unverified_context()]
    last_error: Exception | None = None
    for context in contexts:
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                content_type = response.headers.get("Content-Type", "")
                allowed_types = ("text/html", "application/xhtml", "application/xml", "text/xml", "text/plain")
                if content_type and not any(kind in content_type for kind in allowed_types):
                    return ""
                raw = response.read(1_500_000)
                encoding = response.headers.get("Content-Encoding", "").lower()
            if encoding == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", errors="ignore")
        except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    return ""


def fetch_with_curl(url: str, timeout: float) -> str:
    max_time = max(int(timeout), 3)
    command = [
        "curl",
        "-L",
        "--silent",
        "--show-error",
        "--compressed",
        "--connect-timeout",
        str(max_time),
        "--max-time",
        str(max_time + 4),
        "--retry",
        "1",
        "--retry-delay",
        "1",
        "-A",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "-H",
        "Accept-Language: en-US,en;q=0.9,it;q=0.8,de;q=0.8,fr;q=0.7",
        "-H",
        "Cookie: cookieyes-consent=consentid:yes,action:yes,necessary:yes,functional:yes,analytics:yes,advertisement:yes; cookie_notice_accepted=true; viewed_cookie_policy=yes",
        url,
    ]
    result = subprocess.run(command, text=True, capture_output=True, timeout=max_time + 6)
    if result.returncode != 0:
        raise urllib.error.URLError(result.stderr.strip() or f"curl failed with {result.returncode}")
    return result.stdout


def scrape_site(
    base_url: str,
    max_fetch_attempts: int = MAX_FETCH_ATTEMPTS,
    fetch_timeout: float = DEFAULT_FETCH_TIMEOUT,
    max_elapsed_seconds: float = 0,
) -> dict:
    base = base_url.rstrip("/")
    started_at = time.monotonic()
    pages: list[str] = []
    emails: set[str] = set()
    phones: set[str] = set()
    bases = candidate_bases(base)
    paths = list(CONTACT_PATHS)
    initial_path = path_from_url(base)
    if initial_path and initial_path not in paths:
        paths.insert(0, initial_path)
    extra_urls: list[str] = []
    seen_extra_urls: set[str] = set()
    attempts = 0
    sitemap_checked = False

    path_index = 0
    extra_url_index = 0
    while path_index < len(paths) or extra_url_index < len(extra_urls):
        if max_elapsed_seconds and time.monotonic() - started_at >= max_elapsed_seconds:
            break
        if len(pages) >= MAX_PAGES:
            break
        if attempts >= max_fetch_attempts:
            break
        markup = ""
        used_base = base
        if path_index < len(paths):
            path = paths[path_index]
            path_index += 1
            for candidate_base in bases:
                if attempts >= max_fetch_attempts:
                    break
                url = urllib.parse.urljoin(candidate_base + "/", path.lstrip("/"))
                attempts += 1
                try:
                    markup = fetch_url(url, timeout=fetch_timeout)
                    used_base = candidate_base
                    break
                except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError):
                    continue
        else:
            url = extra_urls[extra_url_index]
            extra_url_index += 1
            attempts += 1
            try:
                markup = fetch_url(url, timeout=fetch_timeout)
                parsed_url = urllib.parse.urlparse(url)
                used_base = f"{parsed_url.scheme}://{parsed_url.netloc}"
            except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError):
                markup = ""
        if not markup:
            continue

        parser = TextExtractor()
        parser.feed(markup)
        text = html.unescape(" ".join(parser.text_parts))
        pages.append(text)
        if not sitemap_checked:
            sitemap_checked = True
            for sitemap_path in sitemap_contact_paths(used_base):
                insert_path_candidate(paths, path_index, sitemap_path)
        for link in parser.links:
            maybe_path = contact_path_from_link(used_base, link)
            if maybe_path:
                insert_path_candidate(paths, path_index, maybe_path)
            maybe_url = external_contact_url_from_link(used_base, link)
            if maybe_url and maybe_url not in seen_extra_urls and len(extra_urls) < 20:
                seen_extra_urls.add(maybe_url)
                insert_url_candidate(extra_urls, extra_url_index, maybe_url)

        for email in extract_emails(markup, text, parser.links):
            lowered = email.lower().strip(".")
            if not any(part in lowered for part in BAD_EMAIL_PARTS):
                emails.add(lowered)

        for phone in extract_phones(text):
            phones.add(phone)

        if emails and len(pages) >= 2:
            break
        if len(" ".join(pages)) > 40_000:
            break

    page_text = compact_text(" ".join(pages), 12_000)
    if not page_text and emails:
        page_text = f"Email contacts found on website: {', '.join(sorted(emails))}"
    if not page_text:
        raise RuntimeError(f"No readable HTML text fetched after {attempts} fetch attempts")

    return {
        "page_text": page_text,
        "emails": sorted(emails),
        "phones": sorted(phones),
    }


def candidate_bases(base_url: str) -> list[str]:
    parsed = urllib.parse.urlparse(base_url)
    scheme = parsed.scheme or "https"
    host = parsed.netloc or parsed.path
    host = host.strip("/")
    hosts = [host]
    if host.startswith("www."):
        hosts.append(host[4:])
    else:
        hosts.append("www." + host)
    schemes = [scheme, "http" if scheme == "https" else "https"]
    bases: list[str] = []
    for item_scheme in schemes:
        for item_host in hosts:
            candidate = f"{item_scheme}://{item_host}"
            if candidate not in bases:
                bases.append(candidate)
    return bases


def path_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or ""
    if not path or path == "/":
        return ""
    return path + (("?" + parsed.query) if parsed.query else "")


def insert_path_candidate(paths: list[str], current_index: int, candidate: str) -> None:
    if not candidate or candidate in paths:
        return
    priority = link_priority(candidate)
    insert_at = current_index
    while insert_at < len(paths) and link_priority(paths[insert_at]) > priority:
        insert_at += 1
    paths.insert(insert_at, candidate)


def insert_url_candidate(urls: list[str], current_index: int, candidate: str) -> None:
    if not candidate or candidate in urls:
        return
    priority = link_priority(candidate)
    insert_at = current_index
    while insert_at < len(urls) and link_priority(urls[insert_at]) > priority:
        insert_at += 1
    urls.insert(insert_at, candidate)


def link_priority(value: str) -> int:
    lowered = urllib.parse.unquote(value).lower()
    if any(hint in lowered for hint in LOW_VALUE_LINK_HINTS):
        return -10
    if any(hint in lowered for hint in HIGH_VALUE_LINK_HINTS):
        return 100
    if any(hint in lowered for hint in MEDIUM_VALUE_LINK_HINTS):
        return 60
    return 0


def compact_text(text: str, limit: int) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean[:limit]


def extract_emails(markup: str, text: str, links: list[str]) -> set[str]:
    haystack = html.unescape(urllib.parse.unquote(markup + " " + text + " " + " ".join(links)))
    emails = set(EMAIL_RE.findall(haystack))
    tag_stripped = html.unescape(urllib.parse.unquote(HTML_TAG_RE.sub(" ", markup)))
    compacted = normalize_email_spacing(haystack + " " + tag_stripped)
    emails.update(EMAIL_RE.findall(compacted))
    emails.update(extract_split_emails(compacted))
    for mail in MAILTO_HREF_RE.findall(markup):
        decoded_mail = html.unescape(urllib.parse.unquote(mail))
        emails.update(EMAIL_RE.findall(decoded_mail))
    for link in links:
        decoded_link = html.unescape(urllib.parse.unquote(link))
        if decoded_link.lower().startswith("mailto:"):
            mail = decoded_link.split(":", 1)[1].split("?", 1)[0]
            emails.update(EMAIL_RE.findall(mail))
    for local, domain, tld in OBFUSCATED_EMAIL_RE.findall(haystack):
        emails.add(f"{local}@{domain}.{tld}")
    for local, domain in OBFUSCATED_AT_RE.findall(haystack):
        emails.add(f"{local}@{domain}")
    emails.update(extract_javascript_emails(markup))
    for encoded in CF_EMAIL_RE.findall(markup):
        decoded = decode_cfemail(encoded)
        if decoded:
            emails.add(decoded)
    return remove_suffix_email_false_positives(emails)


def extract_javascript_emails(markup: str) -> set[str]:
    decoded_markup = html.unescape(markup)
    variables: dict[str, str] = {}
    found: set[str] = set()
    for name, expression in JS_ASSIGNMENT_RE.findall(decoded_markup):
        value = "".join(part_a or part_b for part_a, part_b in JS_STRING_RE.findall(expression))
        if re.match(rf"\s*{re.escape(name)}\s*\+", expression):
            value = variables.get(name, "") + value
        variables[name] = html.unescape(value)
        found.update(EMAIL_RE.findall(variables[name]))
    return found


def extract_phones(text: str) -> set[str]:
    phones: set[str] = set()
    phone_context_words = (
        "phone",
        "telephone",
        "tel",
        "mobile",
        "movil",
        "móvil",
        "fono",
        "telefono",
        "teléfono",
        "fax",
    )
    for match in PHONE_RE.finditer(text):
        raw = match.group(0)
        candidate = re.sub(r"\s+", " ", raw).strip(" .,-")
        digits = re.sub(r"\D", "", candidate)
        context = text[max(0, match.start() - 80) : match.end() + 40].lower()
        has_phone_context = any(word in context for word in phone_context_words)
        if 7 <= len(digits) <= 16 and (candidate.startswith("+") or has_phone_context):
            phones.add(candidate)
    return remove_suffix_phone_false_positives(phones)


def remove_suffix_phone_false_positives(phones: set[str]) -> set[str]:
    cleaned = set(phones)
    phone_digits = {phone: re.sub(r"\D", "", phone) for phone in phones}
    for phone, digits in phone_digits.items():
        for other, other_digits in phone_digits.items():
            if (
                phone != other
                and len(other_digits) >= len(digits) + 2
                and other_digits.endswith(digits)
            ):
                cleaned.discard(phone)
                break
    return cleaned


def remove_suffix_email_false_positives(emails: set[str]) -> set[str]:
    cleaned = set(emails)
    lowered = {email.lower(): email for email in emails}
    for email in list(lowered):
        local, _, domain = email.partition("@")
        if not local or not domain:
            continue
        for other in lowered:
            other_local, _, other_domain = other.partition("@")
            if (
                other != email
                and other_domain == domain
                and len(other_local) >= len(local) + 3
                and other_local.endswith(local)
            ):
                cleaned.discard(lowered[email])
                break
    return cleaned


def normalize_email_spacing(value: str) -> str:
    compacted = re.sub(r"\s*@\s*", "@", value)
    compacted = re.sub(r"(?<=[A-Z0-9-])\s*\.\s*(?=[A-Z]{2,}\b)", ".", compacted, flags=re.IGNORECASE)
    compacted = re.sub(r"(?<=@)\s+", "", compacted)
    compacted = re.sub(r"\s+(?=[A-Z0-9.-]+\.[A-Z]{2,})", "", compacted, flags=re.IGNORECASE)
    return compacted


def extract_split_emails(value: str) -> set[str]:
    found: set[str] = set()
    prefix_tokens = {"info", "sales", "export", "contact", "office", "admin", "commerciale", "vendite"}
    for match in re.finditer(r"(.{0,80})@\s*([A-Z0-9.-]+\s*\.\s*[A-Z]{2,})", value, re.IGNORECASE):
        before = match.group(1)
        domain = re.sub(r"\s+", "", match.group(2))
        tokens = [token for token in re.findall(r"[A-Z0-9._%+-]+", before, flags=re.IGNORECASE) if token.lower() != "mailto"]
        combined_candidates = []
        if len(tokens) >= 2 and tokens[-2].lower() in prefix_tokens:
            combined_candidates.append(tokens[-2] + tokens[-1])
        if len(tokens) >= 3 and tokens[-3].lower() in prefix_tokens:
            combined_candidates.append(tokens[-3] + tokens[-2] + tokens[-1])
        candidates = combined_candidates
        if not candidates and tokens:
            candidates.append(tokens[-1])
        for local in candidates:
            candidate = f"{local}@{domain}"
            if EMAIL_RE.fullmatch(candidate):
                found.add(candidate)
    return found


def decode_cfemail(encoded: str) -> str:
    try:
        key = int(encoded[:2], 16)
        chars = [chr(int(encoded[i : i + 2], 16) ^ key) for i in range(2, len(encoded), 2)]
        return "".join(chars)
    except (ValueError, IndexError):
        return ""


def contact_path_from_link(base_url: str, link: str) -> str:
    parsed_base = urllib.parse.urlparse(base_url)
    absolute = urllib.parse.urljoin(base_url.rstrip("/") + "/", link)
    parsed = urllib.parse.urlparse(absolute)
    if parsed.netloc and parsed.netloc != parsed_base.netloc:
        return ""
    path = parsed.path or ""
    lowered = path.lower()
    if any(hint in lowered for hint in LOW_VALUE_LINK_HINTS):
        return ""
    if any(hint in lowered for hint in CONTACT_LINK_HINTS):
        return path + (("?" + parsed.query) if parsed.query else "")
    return ""


def external_contact_url_from_link(base_url: str, link: str) -> str:
    parsed_base = urllib.parse.urlparse(base_url)
    absolute = urllib.parse.urljoin(base_url.rstrip("/") + "/", link)
    parsed = urllib.parse.urlparse(absolute)
    if not parsed.scheme.startswith("http") or not parsed.netloc:
        return ""
    if parsed.netloc == parsed_base.netloc:
        return ""
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if any(host == domain or host.endswith("." + domain) for domain in SKIP_EXTERNAL_DOMAINS):
        return ""
    path = parsed.path or ""
    lowered = (path + " " + parsed.query).lower()
    if any(hint in lowered for hint in LOW_VALUE_LINK_HINTS):
        return ""
    if any(hint in lowered for hint in CONTACT_LINK_HINTS):
        return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))
    return ""


def sitemap_contact_paths(base_url: str) -> list[str]:
    paths: list[str] = []
    for sitemap_path in ("/sitemap.xml", "/sitemap_index.xml"):
        url = urllib.parse.urljoin(base_url.rstrip("/") + "/", sitemap_path.lstrip("/"))
        try:
            markup = fetch_url(url, timeout=SITEMAP_FETCH_TIMEOUT)
        except Exception:
            continue
        for match in re.findall(r"<loc>\s*([^<]+)\s*</loc>", markup, flags=re.IGNORECASE):
            decoded = html.unescape(match.strip())
            maybe_path = contact_path_from_link(base_url, decoded)
            if maybe_path and maybe_path not in paths:
                paths.append(maybe_path)
    return paths[:20]
