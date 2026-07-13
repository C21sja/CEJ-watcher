import hashlib
from urllib.parse import urlencode

from housing_policy import (
    canonical_listing_key,
    contains_commercial_use,
    contains_restricted_eligibility,
    extract_amount,
    extract_postcode,
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
