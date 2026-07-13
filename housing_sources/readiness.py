import hashlib
import re
from html import unescape
from urllib.parse import urlencode, urljoin, urlparse

from housing_policy import (
    canonical_listing_key,
    contains_commercial_use,
    contains_restricted_eligibility,
    extract_amount,
    extract_postcode,
    is_preferred_postcode,
    listing_matches_policy,
    normalize_text,
)
from housing_sources import SourceContractError, SourceSnapshot

RLE_QUERY = '*[_id=="0f753ee8-70a8-4ee0-99e5-b33ca61f67ce"][0]{_id,_updatedAt,title,slug,content}'
RLE_URL = "https://k56dk3dw.api.sanity.io/v2025-07-23/data/query/production?" + urlencode(
    {"query": RLE_QUERY}
)


def _flatten(value):
    if isinstance(value, dict):
        return " ".join(
            _flatten(item)
            for key, item in value.items()
            if not str(key).startswith("_") and key not in {"asset", "image", "metadata"}
        )
    if isinstance(value, list):
        return " ".join(_flatten(item) for item in value)
    return str(value or "")


def _signature(value):
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


def _readiness_event(
    event_id,
    source,
    headline,
    description,
    signature,
    url,
    urgent=False,
    registration_closed=False,
):
    return {
        "id": event_id,
        "source": source,
        "headline": headline,
        "description": description,
        "signature": signature,
        "url": url,
        "urgent": urgent,
        "registration_closed": registration_closed,
    }


def parse_rle_document(document):
    if not isinstance(document, dict) or "content" not in document:
        raise SourceContractError("RLE Sanity document has no content")
    content = document["content"]
    full_text = _flatten(content)
    normalized_full = normalize_text(full_text)
    no_vacancies = "ingen ledige ejendomme" in normalized_full
    listings = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("_type") != "vacancy":
            continue
        use = normalize_text(block.get("use"))
        status_text = normalize_text(block.get("status"))
        if use not in {"bolig", "privat bolig", "lejlighed"} or status_text not in {
            "ledig",
            "available",
        }:
            continue
        restriction_text = " ".join(
            str(block.get(field) or "") for field in ("eligibility", "requirements", "description")
        )
        if contains_commercial_use(f"type {block.get('use', '')}") or contains_restricted_eligibility(
            restriction_text
        ):
            continue
        postcode = extract_postcode(block.get("postalCode"))
        price = extract_amount(block.get("monthlyRent"))
        address = f"{block.get('address', '')}, {block.get('postalCode', '')} {block.get('city', '')}".strip(
            ", "
        )
        listing = {
            "id": f"rle:{block.get('_key') or normalize_text(address)}",
            "status": "Available",
            "name": address,
            "price": {"amount": price},
            "location": {"formatted": address},
            "availableFrom": "See link for info",
            "url": "https://rle.dk/ledige-ejendomme",
            "source": "RLE",
            "transaction_type": "rent",
            "price_period": "month",
            "canonical_key": canonical_listing_key(address, "rent"),
            "raw_text": restriction_text,
            "source_priority": 20,
        }
        if postcode and listing_matches_policy(listing):
            listings.append(listing)
    if no_vacancies and listings:
        raise SourceContractError(
            "RLE document contradicts itself with vacancies and a no-vacancy statement"
        )
    if listings:
        return SourceSnapshot(source="RLE", listings=listings)
    if no_vacancies:
        event = _readiness_event(
            "readiness:rle",
            "RLE",
            "No residential vacancies",
            "RLE currently states that it has no vacant properties.",
            _signature("no residential vacancies"),
            "https://rle.dk/ledige-ejendomme",
        )
    else:
        event = _readiness_event(
            "readiness:rle",
            "RLE",
            "RLE changed - inspect now",
            "The no-vacancy statement changed, but no safe residential card could be parsed.",
            _signature(full_text),
            "https://rle.dk/ledige-ejendomme",
            urgent=True,
        )
        event["kind"] = "inspection"
    return SourceSnapshot(source="RLE", events=[event])


def fetch_rle(fetch_json):
    data = fetch_json(RLE_URL)
    document = data.get("result") if isinstance(data, dict) else None
    return parse_rle_document(document)


CPH_DOCUMENT_URLS = {
    "home": "https://cphhomes.dk/",
    "holmen": "https://cphhomes.dk/holmen/",
    "sydhavnen": "https://cphhomes.dk/sydhavnen/",
    "orestaden": "https://cphhomes.dk/orestaden/",
    "bryggen": "https://cphhomes.dk/bryggen/",
    "engholmene": "https://cphhomes.dk/engholmene/",
}
CPH_AVAILABILITY_TERMS = (
    "ledig",
    "udlejes",
    "husleje",
    "book fremvisning",
    "ansog",
    "skriv dig op",
    "tilmelding",
    "interesseliste",
    "boliger til leje",
)
CPH_EXTERNAL_ACTION_HOSTS = frozenset()  # Add only exact hosts backed by a captured live application link.
CPH_MAX_DISCOVERED_POSTS = 50


def _cph_main(html):
    main = re.search(r"<main\b[^>]*>([\s\S]*?)</main\s*>", html, re.IGNORECASE)
    if not main:
        raise SourceContractError("CPH Homes page has no main content container")
    return re.sub(
        r"<script[\s\S]*?</script\s*>|<style[\s\S]*?</style\s*>",
        " ",
        main.group(1),
        flags=re.IGNORECASE,
    )


def _safe_cph_link(base_url, href):
    absolute = urljoin(base_url, unescape(href))
    parsed = urlparse(absolute)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"cphhomes.dk", "www.cphhomes.dk"}
        or parsed.port not in {None, 443}
    ):
        return None
    return parsed._replace(query="", fragment="").geturl()


def discover_cphomes_post_urls(home_html):
    content = _cph_main(home_html)
    discovered = set()
    for article in re.findall(r"<article\b[^>]*>([\s\S]*?)</article\s*>", content, re.IGNORECASE):
        for href in re.findall(r'href=["\']([^"\']+)["\']', article, re.IGNORECASE):
            safe_link = _safe_cph_link(CPH_DOCUMENT_URLS["home"], href)
            if safe_link and safe_link not in CPH_DOCUMENT_URLS.values():
                discovered.add(safe_link)
    if len(discovered) > CPH_MAX_DISCOVERED_POSTS:
        raise SourceContractError("CPH Homes article discovery exceeded its defensive limit")
    return sorted(discovered)


def _cph_plain_html(value):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _cph_event(key, url, html):
    content = _cph_main(html)
    main_text = _cph_plain_html(content)
    normalized = normalize_text(f"{key} {main_text}")
    matched_terms = {term for term in CPH_AVAILABILITY_TERMS if term in normalized}
    signals = {f"term:{term}" for term in matched_terms}
    for amount in re.findall(r"\d[\d.\s]*\s*kr", main_text, re.IGNORECASE):
        signals.add(f"price:{extract_amount(amount)}")
    for postcode in re.findall(r"\b\d{4}\b", main_text):
        if is_preferred_postcode(int(postcode)):
            signals.add(f"postcode:{postcode}")
    for href, label in re.findall(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a\s*>', content, re.IGNORECASE
    ):
        if any(term in normalize_text(label) for term in CPH_AVAILABILITY_TERMS):
            absolute = urljoin(url, unescape(href))
            parsed = urlparse(absolute)
            if parsed.scheme != "https" or not parsed.hostname or parsed.port not in {None, 443}:
                continue
            if (
                parsed.hostname in {"cphhomes.dk", "www.cphhomes.dk"}
                or parsed.hostname in CPH_EXTERNAL_ACTION_HOSTS
            ):
                signals.add(f"application-link:{absolute}")
            else:
                signals.add(f"external-application-review:{parsed.hostname}")
    has_evidence = bool(matched_terms)
    headline = (
        "CPH Homes availability signal - inspect now" if has_evidence else "CPH Homes monitoring ready"
    )
    event = _readiness_event(
        f"readiness:cphhomes:{key}",
        "CPH Homes",
        headline,
        (
            "This relevant page contains availability evidence; inspect it before applying."
            if has_evidence
            else "This relevant CPH Homes page is being monitored for material changes."
        ),
        _signature(f"{normalized}|{'|'.join(sorted(signals))}"),
        url,
        urgent=False,
    )
    event.update(
        {
            "signals": sorted(signals),
            "baseline_headline": headline,
            "change_headline": (
                "CPH Homes availability signal - inspect now"
                if has_evidence
                else "CPH Homes changed - inspect now"
            ),
            "kind": "inspection",
        }
    )
    return event


def parse_cphomes_documents(documents, discovered_urls=()):
    if not isinstance(documents, dict):
        raise SourceContractError("CPH Homes documents must be a URL-to-HTML mapping")
    key_by_url = {url: key for key, url in CPH_DOCUMENT_URLS.items()}
    for url in discovered_urls:
        safe_url = _safe_cph_link(CPH_DOCUMENT_URLS["home"], url)
        if safe_url != url:
            raise SourceContractError(
                "CPH Homes discovered post URL is not an exact same-host HTTPS URL"
            )
        slug = urlparse(url).path.strip("/").replace("/", ":")
        key_by_url[url] = f"post:{slug or hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]}"
    events = [
        _cph_event(key_by_url[url], url, html)
        for url, html in documents.items()
        if url in key_by_url
    ]
    return SourceSnapshot(source="CPH Homes", events=sorted(events, key=lambda event: event["id"]))


def fetch_cphomes(fetch_text):
    documents = {url: fetch_text(url) for url in CPH_DOCUMENT_URLS.values()}
    post_urls = discover_cphomes_post_urls(documents[CPH_DOCUMENT_URLS["home"]])
    documents.update({url: fetch_text(url) for url in post_urls})
    return parse_cphomes_documents(documents, post_urls)
