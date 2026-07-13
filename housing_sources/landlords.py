import re
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit

from housing_policy import (
    canonical_listing_key,
    contains_restricted_eligibility,
    extract_amount,
    listing_matches_policy,
    normalize_text,
)
from housing_sources import SourceContractError, SourceSnapshot


TAURUS_URL = "https://www.taurus.dk/boligudlejning/ledige-lejemal/"


def _plain_text(value):
    value = str(value or "")
    value = re.sub(
        r"<(?:script|style)\b[^>]*>[\s\S]*?</(?:script|style)\s*>",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _attribute(attributes, name):
    match = re.search(
        rf"(?<![\w:-]){re.escape(name)}\s*=\s*([\"'])(.*?)\1",
        attributes,
        re.IGNORECASE | re.DOTALL,
    )
    return unescape(match.group(2)) if match else ""


def parse_taurus_overview(html):
    cards = []
    for match in re.finditer(
        r"<a\b(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</a\s*>",
        str(html or ""),
        re.IGNORECASE,
    ):
        attrs = match.group("attrs")
        if "rental-item" not in _attribute(attrs, "class").lower():
            continue
        cards.append(attrs)

    if not cards:
        visible_text = normalize_text(_plain_text(html))
        if "ingen ledige" in visible_text:
            return []
        raise SourceContractError("Taurus rental cards are missing")

    candidates = []
    for attrs in cards:
        href = _attribute(attrs, "href")
        record_id = parse_qs(urlsplit(href).query).get("id", [""])[0].strip()
        price = extract_amount(_attribute(attrs, "data-price"))
        direct_url = urljoin(TAURUS_URL, href)
        direct_parts = urlsplit(direct_url)
        if (
            not record_id
            or price is None
            or direct_parts.scheme != "https"
            or direct_parts.hostname != "www.taurus.dk"
        ):
            continue
        candidates.append(
            {
                "record_id": record_id,
                "url": direct_url,
                "price": price,
                "rooms": extract_amount(_attribute(attrs, "data-rooms")),
                "size_sqm": extract_amount(
                    _attribute(attrs, "data-living-area")
                ),
            }
        )
    return candidates


def _label_value(html, label):
    label_variants = (label,)
    if label == "Målgruppe":
        label_variants = (
            "Målgruppe",
            "Maalgruppe",
            "M&aring;lgruppe",
            "MÃ¥lgruppe",
        )

    for variant in label_variants:
        pattern = re.compile(
            rf">\s*{re.escape(variant)}\s*:?\s*</[^>]+>\s*"
            r"(?:<[^>/][^>]*>\s*)*([^<]+)",
            re.IGNORECASE,
        )
        for match in pattern.finditer(html):
            value = _plain_text(match.group(1))
            if value:
                return value
    return ""


def _main_content(html):
    match = re.search(
        r"<main\b[^>]*>([\s\S]*?)</main\s*>",
        str(html or ""),
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


def parse_taurus_detail(candidate, html):
    main = _main_content(html)
    status_text = _label_value(main, "Status")
    rent_text = _label_value(main, "Husleje")
    street = _label_value(main, "Vejnavn")
    house_number = _label_value(main, "Husnummer")
    postcode = _label_value(main, "Postnummer")
    city = _label_value(main, "By")
    current_rent = extract_amount(rent_text)

    if not all((status_text, rent_text, street, house_number, postcode, city)):
        raise SourceContractError(
            f"Taurus detail {candidate['record_id']} is missing labelled fields"
        )
    if current_rent is None:
        raise SourceContractError(
            f"Taurus detail {candidate['record_id']} has an invalid labelled rent"
        )

    normalized_status = normalize_text(status_text)
    if normalized_status == "ledig":
        status = "Available"
    elif normalized_status in {"reserveret", "udlejet", "under kontrakt"}:
        status = "Reserved"
    else:
        return None

    study_value = _label_value(main, "Studiebolig")
    audience = _label_value(main, "Målgruppe")
    requirements = _label_value(main, "Krav")
    restrictions = []
    normalized_study = normalize_text(study_value)
    if normalized_study and normalized_study not in {"nej", "no", "false", "0"}:
        restrictions.append(f"Studiebolig {study_value}")
    restrictions.extend(value for value in (audience, requirements) if value)
    restriction_text = " ".join(restrictions)

    address = f"{street} {house_number}, {postcode} {city}".strip()
    listing = {
        "id": f"taurus:{candidate['record_id']}",
        "status": status,
        "name": address,
        "price": {"amount": current_rent},
        "location": {"formatted": address},
        "availableFrom": "See link for info",
        "url": candidate["url"],
        "source": "Taurus",
        "transaction_type": "rent",
        "price_period": "month",
        "rooms": candidate["rooms"],
        "size_sqm": candidate["size_sqm"],
        "raw_text": restriction_text,
        "canonical_key": canonical_listing_key(address, "rent"),
        "source_priority": 20,
    }
    if contains_restricted_eligibility(restriction_text):
        return None
    return listing if listing_matches_policy(listing) else None


def fetch_taurus(fetch_text):
    candidates = parse_taurus_overview(fetch_text(TAURUS_URL))
    shortlisted = [
        candidate
        for candidate in candidates
        if 0 < candidate["price"] <= 18_000
    ]
    listings = []
    valid_details = 0
    for candidate in shortlisted:
        try:
            detail_html = fetch_text(candidate["url"])
            listing = parse_taurus_detail(candidate, detail_html)
            valid_details += 1
        except Exception as exc:
            print(f"Taurus detail {candidate['record_id']} failed: {exc}")
            continue
        if listing:
            listings.append(listing)

    if shortlisted and valid_details == 0:
        raise SourceContractError(
            "No shortlisted Taurus detail matched the labelled-field contract"
        )
    return SourceSnapshot(source="Taurus", listings=listings)
