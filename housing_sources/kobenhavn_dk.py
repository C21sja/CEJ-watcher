import hashlib
import re
from html import unescape
from urllib.parse import urlparse

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


def _text(value):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _rows(section):
    return re.findall(r"<tr[^>]*>([\s\S]*?)</tr\s*>", section, re.IGNORECASE)


def _candidate_from_row(row, transaction_type):
    link = re.search(r'href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a\s*>', row, re.IGNORECASE)
    if not link:
        return None
    cells = [_text(cell) for cell in re.findall(r"<td[^>]*>([\s\S]*?)</td\s*>", row, re.IGNORECASE)]
    address = _text(link.group(2))
    price = extract_amount(cells[1]) if len(cells) > 1 else None
    rooms = extract_amount(cells[2]) if len(cells) > 2 else None
    size = extract_amount(cells[3]) if len(cells) > 3 else None
    if not price or not rooms or not size:
        return None
    origin_url = link.group(1)
    origin_host = urlparse(origin_url).netloc.lower().removeprefix("www.")
    origin_id = urlparse(origin_url).path.rstrip("/").split("/")[-1]
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
        "price_limit": 15000 if transaction_type == "rent" else 2800000,
        "price_limit_inclusive": False,
    }
    return candidate if listing_matches_policy(probe) else None


def parse_candidates(html):
    if "Lejligheder til leje" not in html:
        raise SourceContractError("Kobenhavn.dk rental section is missing")
    candidates = []
    headings = list(re.finditer(r"<h4[^>]*>([\s\S]*?)</h4\s*>", html, re.IGNORECASE))
    for index, heading in enumerate(headings):
        title = normalize_text(_text(heading.group(1)))
        end = headings[index + 1].start() if index + 1 < len(headings) else len(html)
        section = html[heading.end() : end]
        if "lejligheder til leje" in title:
            transaction_type = "rent"
        elif "andelsbolig" in title:
            transaction_type = "cooperative_sale"
        else:
            continue
        candidates.extend(
            filter(None, (_candidate_from_row(row, transaction_type) for row in _rows(section)))
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


# Extend this exact-host map only with a captured, tested record extractor.
ORIGIN_VERIFIERS = {"akutbolig.dk": _akutbolig_record}


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
