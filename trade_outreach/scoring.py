from __future__ import annotations

import re
import urllib.parse


VALID_EMAIL_RE = re.compile(
    r"[A-Z0-9](?:[A-Z0-9._%+-]{0,62}[A-Z0-9])?@"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+"
    r"[A-Z]{2,12}",
    re.IGNORECASE,
)
BAD_EMAIL_TLDS = {
    "css",
    "gif",
    "htm",
    "html",
    "http",
    "https",
    "jpeg",
    "jpg",
    "js",
    "php",
    "png",
    "svg",
    "webp",
    "we",
    "xml",
}
LONG_ALLOWED_EMAIL_TLDS = {
    "email",
    "global",
    "group",
    "online",
    "solutions",
    "technology",
}
CONCATENATED_EMAIL_RE = re.compile(
    r"\.[a-z]{2,4}(?:info|sales|contact|office|post|mail|ventas|commercial|biuro)",
    re.IGNORECASE,
)


POSITIVE_TERMS = {
    "steel pipe": 4,
    "steel tubing": 4,
    "tube & pipe": 4,
    "tube and pipe": 4,
    "pipe systems": 4,
    "piping": 3,
    "pipe stockist": 5,
    "pipe stockholder": 5,
    "carbon steel pipe": 5,
    "pipe distributor": 5,
    "pipe supplier": 4,
    "api 5l": 4,
    "astm a53": 3,
    "astm a106": 3,
    "line pipe": 3,
    "seamless pipe": 3,
    "welded pipe": 3,
    "inventory": 2,
    "stocking": 2,
    "distributor": 3,
    "supplier": 2,
    "warehouse": 2,
    "stahlrohr": 5,
    "stahlrohre": 5,
    "rohr": 2,
    "rohre": 2,
    "rohrhandel": 5,
    "rohrleitung": 3,
    "rohrleitungen": 3,
    "geschweißte stahlrohre": 5,
    "nahtlose stahlrohre": 5,
    "edelstahlrohre": 4,
    "lagerhalter": 5,
    "lagerhaltung": 3,
    "lagerprogramm": 3,
    "projektgeschäft": 3,
    "flansche": 2,
    "fittings": 2,
}

NEGATIVE_TERMS = {
    "job opening": -4,
    "careers": -2,
    "blog": -1,
    "plumbing contractor": -3,
    "construction services": -2,
    "news": -1,
}

LOCATION_HINTS = [
    "europe",
    "eu",
    "european",
    "germany",
    "deutschland",
    "netherlands",
    "holland",
    "belgium",
    "france",
    "italy",
    "spain",
    "poland",
    "czech",
    "romania",
    "austria",
    "sweden",
    "norway",
    "denmark",
    "finland",
    "uk",
    "united kingdom",
    "ireland",
    "usa",
    "united states",
    "houston",
    "chicago",
]


def score_lead(page_text: str, emails: list[str]) -> tuple[int, str]:
    text = (page_text or "").lower()
    score = 0
    reasons: list[str] = []

    for term, points in POSITIVE_TERMS.items():
        if term in text:
            score += points
            reasons.append(f"+{points} {term}")

    for term, points in NEGATIVE_TERMS.items():
        if term in text:
            score += points
            reasons.append(f"{points} {term}")

    if emails:
        score += 2
        reasons.append("+2 public email")

    if any(hint in text for hint in LOCATION_HINTS) or re.search(r"\b[A-Z]{2}\s+\d{5}\b", page_text or ""):
        score += 2
        reasons.append("+2 location signal")

    if has_business_email(emails):
        score += 2
        reasons.append("+2 business mailbox")

    return max(score, 0), "; ".join(reasons[:12])


def has_business_email(emails: list[str]) -> bool:
    return any(is_generic_business_email(email) for email in emails)


def pick_recipient(emails: list[str]) -> str | None:
    emails = sorted(set(email.lower().strip() for email in emails if email))
    if not emails:
        return None
    priority = ("sales@", "vertrieb@", "export@", "quotes@", "quote", "info@", "contact@")
    for prefix in priority:
        for email in emails:
            if email.startswith(prefix):
                return email
    return emails[0]


def is_generic_business_email(email: str) -> bool:
    lowered = email.lower().strip()
    prefixes = (
        "sales@",
        "vertrieb@",
        "export@",
        "info@",
        "contact@",
        "office@",
        "service@",
        "quotes@",
        "quote",
        "inquiry@",
        "enquiry@",
        "anfrage@",
        "kontakt@",
    )
    return lowered.startswith(prefixes)


BAD_EMAIL_DOMAINS = {
    "google.com",
    "schema.org",
    "example.com",
    "sentry.io",
    "wixpress.com",
    "wordpress.com",
}

BAD_EMAIL_PREFIXES = (
    "noreply@",
    "no-reply@",
    "donotreply@",
    "do-not-reply@",
    "privacy@",
    "legal@",
    "webmaster@",
)


def clean_emails_for_website(emails: list[str], website: str) -> list[str]:
    cleaned: list[str] = []
    for email in emails:
        lowered = email.lower().strip().strip(".")
        if not is_valid_business_email(lowered):
            continue
        domain = lowered.rsplit("@", 1)[1]
        if domain in BAD_EMAIL_DOMAINS:
            continue
        if any(lowered.startswith(prefix) for prefix in BAD_EMAIL_PREFIXES):
            continue
        cleaned.append(lowered)
    return sorted(set(cleaned))


def is_valid_business_email(email: str) -> bool:
    lowered = email.lower().strip().strip(".")
    if not lowered or not VALID_EMAIL_RE.fullmatch(lowered):
        return False
    local, _, domain = lowered.partition("@")
    if not local or not domain or ".." in lowered:
        return False
    if not local[0].isalpha():
        return False
    if local.strip("+._%-") == "":
        return False
    if CONCATENATED_EMAIL_RE.search(local):
        return False
    tld = domain.rsplit(".", 1)[-1]
    if tld in BAD_EMAIL_TLDS:
        return False
    if len(tld) > 4 and tld not in LONG_ALLOWED_EMAIL_TLDS:
        return False
    if any(part in domain for part in {"/", "\\", ":", "?"}):
        return False
    return True


def domain_of(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower() or parsed.path.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def same_or_related_domain(email_domain: str, website_domain: str) -> bool:
    return (
        email_domain == website_domain
        or email_domain.endswith("." + website_domain)
        or website_domain.endswith("." + email_domain)
    )
