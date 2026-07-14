import hashlib
import re
from html import unescape
from urllib.parse import urljoin, urlparse

from housing_policy import (
    canonical_listing_key,
    extract_amount,
    extract_postcode,
    listing_matches_policy,
    normalize_text,
)
from housing_sources import SourceContractError, SourceSnapshot

PAGE_URL = "https://www.kobenhavn.dk/bolig"
NEGATIVE_MARKERS = ("ikke til salg", "solgt", "udlejet", "ikke laengere aktiv", "fjernet")

# Row markup is verified live: each listing is a `<div class="row coloN">`
# (N is numeric, unlike the `coloh` header/label rows) containing plain
# `<div class="col-xs-...">` cells rather than an HTML table. Rental rows
# put the origin link in the first (address) cell; cooperative-sale rows
# put it in the last ("Mægler"/broker) cell, and their address cell omits
# the postcode/city, which instead comes from the section heading.
_ROW_START = re.compile(r'<div class="row colo\d', re.IGNORECASE)
_HEADING = re.compile(r"<h4[^>]*>([\s\S]*?)</h4\s*>", re.IGNORECASE)
_CELL = re.compile(r'<div class="col-xs-\d[^"]*"[^>]*>([\s\S]*?)</div\s*>', re.IGNORECASE)
_SALE_HEADING = re.compile(r"(\d{4})\s+(.+?)\s*-\s*andelsbolig", re.IGNORECASE)


def _text(value):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _rows(section):
    starts = [match.start() for match in _ROW_START.finditer(section)]
    ends = starts[1:] + [len(section)]
    return [section[start:end] for start, end in zip(starts, ends)]


def _absolute(href):
    return urljoin(PAGE_URL, href)


def _candidate_from_row(row, transaction_type, location_suffix=None):
    cells = _CELL.findall(row)
    if len(cells) < 4:
        return None
    address_text = _text(cells[0])
    if not address_text:
        return None
    address = f"{address_text}, {location_suffix}" if location_suffix else address_text

    origin_cell = cells[-1] if transaction_type == "cooperative_sale" else cells[0]
    link = re.search(r'href=["\']([^"\']+)["\']', origin_cell, re.IGNORECASE)
    if not link:
        return None
    origin_url = _absolute(unescape(link.group(1)))
    parsed_origin = urlparse(origin_url)
    if parsed_origin.scheme != "https" or parsed_origin.netloc.lower() in {
        "www.kobenhavn.dk",
        "kobenhavn.dk",
    }:
        return None

    price = extract_amount(_text(cells[1]))
    rooms = extract_amount(_text(cells[2]))
    size = extract_amount(_text(cells[3]))
    if not price or not rooms or not size:
        return None

    origin_host = parsed_origin.netloc.lower().removeprefix("www.")
    origin_id = parsed_origin.path.rstrip("/").split("/")[-1] or origin_host
    candidate = {
        "candidate_id": f"{transaction_type}:{origin_host}:{origin_id}",
        "origin_host": origin_host,
        "origin_id": origin_id,
        "origin_url": origin_url,
        "address": address,
        "postcode": extract_postcode(address),
        "price": price,
        "rooms": rooms,
        "size_sqm": size,
        "transaction_type": transaction_type,
        "price_limit": 15000 if transaction_type == "rent" else 2800000,
        "price_limit_inclusive": False,
    }
    probe = {
        "name": address,
        "location": {"formatted": address},
        "price": {"amount": price},
        "transaction_type": transaction_type,
        "price_limit": candidate["price_limit"],
        "price_limit_inclusive": False,
    }
    return candidate if listing_matches_policy(probe) else None


def parse_candidates(html):
    if "Lejligheder til leje" not in html:
        raise SourceContractError("Kobenhavn.dk rental section is missing")
    candidates = []
    headings = list(_HEADING.finditer(html))
    for index, heading in enumerate(headings):
        title = _text(heading.group(1))
        end = headings[index + 1].start() if index + 1 < len(headings) else len(html)
        section = html[heading.end() : end]
        normalized_title = normalize_text(title)
        sale_match = _SALE_HEADING.match(title)
        if "lejligheder til leje" in normalized_title:
            transaction_type, location_suffix = "rent", None
        elif sale_match:
            transaction_type = "cooperative_sale"
            location_suffix = f"{sale_match.group(1)} {sale_match.group(2)}".strip()
        else:
            continue
        candidates.extend(
            filter(
                None,
                (
                    _candidate_from_row(row, transaction_type, location_suffix)
                    for row in _rows(section)
                ),
            )
        )
    return candidates


def _akutbolig_inventory_url(postcode):
    if 1000 <= postcode <= 1499:
        return "https://www.akutbolig.dk/koebenhavn-k/lejlighed"
    if 1500 <= postcode <= 1799:
        return "https://www.akutbolig.dk/koebenhavn-v"
    if 1800 <= postcode <= 2000:
        return "https://www.akutbolig.dk/frederiksberg/lejlighed"
    slug = {
        2100: "koebenhavn-oe",
        2150: "nordhavn",
        2200: "koebenhavn-n",
        2300: "koebenhavn-s",
        2400: "koebenhavn-nv",
        2450: "koebenhavn-sv",
    }.get(postcode)
    return f"https://www.akutbolig.dk/{slug}" if slug else None


def _address_identity(candidate):
    normalized = normalize_text(candidate["address"])
    postcode = str(candidate.get("postcode") or "")
    return normalized.split(postcode, 1)[0].strip() if postcode and postcode in normalized else normalized


def _metrics_match_when_unit_is_ambiguous(candidate, normalized_record):
    address_identity = _address_identity(candidate)
    has_floor_or_door = bool(re.search(r"\b\d+\s*(?:th|tv|mf|sal|st)\b", address_identity))
    if has_floor_or_door:
        return True
    rooms = candidate.get("rooms")
    size = candidate.get("size_sqm")
    rooms_match = rooms is None or re.search(
        rf"\b{int(rooms)}\s*(?:vaer(?:else|elser)?|rum)\b", normalized_record
    )
    size_match = size is None or re.search(rf"\b{int(size)}\s*m(?:2|²)\b", normalized_record)
    return bool(rooms_match and size_match)


def _current_origin_price(record, transaction_type):
    amounts = [
        extract_amount(value)
        for value in re.findall(
            r"\b\d{1,3}(?:[.\s]\d{3})+\s*(?:kr\.?|,-)|\b\d{4,7}\s*(?:kr\.?|,-)",
            _text(record),
            re.IGNORECASE,
        )
    ]
    if transaction_type == "cooperative_sale":
        amounts = [amount for amount in amounts if amount and amount >= 100_000]
    else:
        amounts = [amount for amount in amounts if amount and amount < 100_000]
    return amounts[0] if amounts else None


def _akutbolig_record(candidate, fetch_text):
    inventory_url = _akutbolig_inventory_url(candidate["postcode"])
    if not inventory_url:
        return None
    html = fetch_text(inventory_url)
    marker = re.search(
        rf'href=["\'][^"\']*/vis/{re.escape(candidate["origin_id"])}(?:[/?#][^"\']*)?["\']',
        html,
        re.IGNORECASE,
    )
    if not marker:
        return None
    next_record = re.search(r'href=["\'][^"\']*/vis/[^"\']+["\']', html[marker.end() :], re.IGNORECASE)
    end = marker.end() + next_record.start() if next_record else min(len(html), marker.start() + 2000)
    return html[marker.start() : end]


# Extend this exact-host map only with a host whose live capture proves a
# working, bounded record extractor. `_akutbolig_record` is kept available
# because it correctly implements the "membership in a rendered inventory
# page" pattern, but a live capture (12 July 2026) found akutbolig.dk has
# since become a client-rendered app with no server-rendered `/vis/{id}`
# markup on its category pages, so it is deliberately NOT registered here;
# registering it would silently fail closed on every candidate. The same
# scan found cooperative-sale rows redirecting to realmaeglerne.dk,
# soeboe-ejendomme.dk, nybolig.dk, eltoftnielsen.dk, danbolig.dk, brikk.dk,
# edc.dk, unikboligsalg.dk, estate.dk, home.dk, and adamschnack.dk. None of
# these twelve hosts has a captured/tested extractor yet, so every current
# candidate falls through to the manual-review path below instead of being
# treated as a verified home. See docs/latest-source-scan.md.
ORIGIN_VERIFIERS = {}


def verify_candidate(candidate, fetch_text):
    parsed_origin = urlparse(candidate["origin_url"])
    if parsed_origin.scheme != "https" or not parsed_origin.netloc:
        return None
    record_fetcher = ORIGIN_VERIFIERS.get(candidate["origin_host"])
    if record_fetcher is None:
        return None
    record = record_fetcher(candidate, fetch_text)
    if not record:
        return None
    normalized_record = normalize_text(_text(record))
    if any(marker in normalized_record for marker in NEGATIVE_MARKERS):
        return None
    if _address_identity(candidate) not in normalized_record:
        return None
    if not _metrics_match_when_unit_is_ambiguous(candidate, normalized_record):
        return None
    current_price = _current_origin_price(record, candidate["transaction_type"])
    if current_price is None:
        return None
    tolerance = (
        max(1_000, int(candidate["price"] * 0.15))
        if candidate["transaction_type"] == "rent"
        else max(50_000, int(candidate["price"] * 0.05))
    )
    if abs(current_price - candidate["price"]) > tolerance:
        return None
    transaction_type = candidate["transaction_type"]
    listing = {
        "id": f"kobenhavn:{candidate['candidate_id']}",
        "status": "Available",
        "name": candidate["address"],
        "price": {"amount": current_price},
        "location": {"formatted": candidate["address"]},
        "availableFrom": "See origin",
        "url": candidate["origin_url"],
        "source": "Kobenhavn.dk (origin verified)",
        "transaction_type": transaction_type,
        "price_period": "total" if transaction_type == "cooperative_sale" else "month",
        "price_limit": 15000 if transaction_type == "rent" else 2800000,
        "price_limit_inclusive": False,
        "canonical_key": canonical_listing_key(candidate["address"], transaction_type),
        "source_priority": 10,
        "rooms": candidate["rooms"],
        "size_sqm": candidate["size_sqm"],
        "raw_text": _text(record),
    }
    return listing if listing_matches_policy(listing) else None


def fetch_kobenhavn(fetch_text):
    candidates = parse_candidates(fetch_text(PAGE_URL))
    listings = []
    diagnostics = []
    for candidate in candidates:
        if candidate["origin_host"] not in ORIGIN_VERIFIERS:
            diagnostics.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "origin_url": candidate["origin_url"],
                    "outcome": "manual_review",
                    "reason": "new origin host has no captured verifier",
                }
            )
            continue
        try:
            verified = verify_candidate(candidate, fetch_text)
        except Exception as exc:
            print(f"Kobenhavn.dk candidate {candidate['candidate_id']} verification failed: {exc}")
            diagnostics.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "origin_url": candidate["origin_url"],
                    "outcome": "error",
                    "reason": str(exc),
                }
            )
            continue
        if verified:
            listings.append(verified)
        else:
            diagnostics.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "origin_url": candidate["origin_url"],
                    "outcome": "suppressed",
                    "reason": "unsupported, inactive, or ambiguous origin",
                }
            )
    review_items = [item for item in diagnostics if item["outcome"] == "manual_review"]
    events = []
    if review_items:
        signals = sorted(
            f"{urlparse(item['origin_url']).netloc.lower()}:{item['candidate_id']}"
            for item in review_items
        )
        events.append(
            {
                "id": "readiness:kobenhavn-dk-origin-review",
                "source": "Kobenhavn.dk",
                "headline": "Kobenhavn.dk has an origin that needs verifier review",
                "description": "; ".join(signals),
                "signature": hashlib.sha256("|".join(signals).encode("utf-8")).hexdigest(),
                "url": PAGE_URL,
                "urgent": False,
                "registration_closed": False,
                "signals": signals,
                "kind": "inspection",
            }
        )
    return SourceSnapshot(source="Kobenhavn.dk", listings=listings, events=events, diagnostics=diagnostics)
