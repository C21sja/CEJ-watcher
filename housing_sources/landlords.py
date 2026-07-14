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
        # Taurus sometimes appends a parenthetical or trailing space to a
        # label (e.g. "Husleje (Mdl.) ", "Boligareal (m2) "), so match the
        # label as a leading word boundary rather than requiring an exact,
        # full string before the closing tag.
        pattern = re.compile(
            rf">\s*{re.escape(variant)}\b[^<]*</[^>]+>\s*:?\s*"
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
    # "By" is not required: a live capture (12 July 2026) found Taurus's
    # sidebar never includes a separate city label, only Postnummer. The
    # shared postcode-based area policy does not need a city name.
    city = _label_value(main, "By")
    current_rent = extract_amount(rent_text)

    if not all((status_text, rent_text, street, house_number, postcode)):
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


LEJE_API_URL = "https://lejeboligmaegleren.dk/Umbraco/Api/Case/Search"
LEJE_UNIT_TYPES_URL = "https://lejeboligmaegleren.dk/Umbraco/Api/Case/UnitCaseTypes"
LEJE_TAGS_URL = "https://lejeboligmaegleren.dk/Umbraco/Api/Case/Tags"


def _lej_state(value):
    normalized = normalize_text(value)
    if normalized in {"ledig", "under opsigelse"}:
        return "Available"
    if normalized in {"kontrakt under udarbejdelse", "udlejet afventer underskrift"}:
        return "Reserved"
    return None


def _lej_dictionary(records):
    if not isinstance(records, list):
        raise SourceContractError("Lejeboligmægleren dictionary response is not a list")
    return {
        record.get("Id"): str(record.get("Name") or "")
        for record in records
        if isinstance(record, dict) and record.get("Id") is not None
    }


def _lej_restriction_text(case, unit_type_names, tag_names):
    tags = case.get("Tags") or []
    tag_text = " ".join(
        str(tag.get("Name") or tag.get("name") or "")
        if isinstance(tag, dict)
        else tag_names.get(tag, str(tag))
        for tag in tags
    )
    unit_type = case.get("UnitType")
    unit_type_text = (
        str(unit_type.get("Name") or unit_type.get("name") or "")
        if isinstance(unit_type, dict)
        else unit_type_names.get(unit_type, str(unit_type or ""))
    )
    return " ".join(
        str(value or "")
        for value in (case.get("Description"), unit_type_text, tag_text)
    )


def fetch_lejeboligmaegleren(post_json, fetch_json, max_pages=50, page_size=24):
    listings = []
    seen_case_ids = set()
    unit_type_names = _lej_dictionary(fetch_json(LEJE_UNIT_TYPES_URL))
    tag_names = _lej_dictionary(fetch_json(LEJE_TAGS_URL))
    for page in range(1, max_pages + 1):
        payload = {
            "PageIndex": page,
            "PageSize": page_size,
            "MaxRent": 18_000,
            "ZipCodes": [],
            "TypeIds": [],
            "TagIds": [],
            "MinRooms": None,
            "MaxRooms": None,
            "MinSize": None,
            "MaxSize": None,
            "MinFloor": None,
            "MaxFloor": None,
            "AcquisitionDateFrom": None,
            "AcquisitionDateTo": None,
            "OnlyAvailable": False,
            "RentalPeriod": None,
            "FacilityIds": [],
            "AddressQuery": "",
        }
        data = post_json(LEJE_API_URL, payload)
        if not isinstance(data, dict) or ("Cases" not in data and "cases" not in data):
            raise SourceContractError("Lejeboligmægleren response has no Cases key")
        cases = data.get("Cases") if "Cases" in data else data.get("cases")
        if not isinstance(cases, list):
            raise SourceContractError("Lejeboligmægleren Cases is not a list")
        if not cases:
            break
        for case in cases:
            case_id = case.get("Id") or case.get("id")
            if not case_id or case_id in seen_case_ids:
                continue
            seen_case_ids.add(case_id)
            address = str(case.get("Address") or case.get("address") or "").strip()
            city_record = case.get("City") or case.get("city") or {}
            if not isinstance(city_record, dict):
                raise SourceContractError(
                    f"Lejeboligmægleren case {case_id} City is not an object"
                )
            postcode = str(city_record.get("ZipCode") or city_record.get("zipCode") or "").strip()
            city = str(city_record.get("Name") or city_record.get("name") or "").strip()
            status = _lej_state(case.get("State") or case.get("state"))
            if status is None:
                continue
            restriction_text = _lej_restriction_text(case, unit_type_names, tag_names)
            address_line = f"{address}, {postcode} {city}".strip(", ")
            listing = {
                "id": f"lejeboligmaegleren:{case_id}",
                "status": status,
                "name": address,
                "price": {"amount": case.get("Rent") or case.get("rent")},
                "location": {"formatted": address_line},
                "availableFrom": case.get("AcquisitionDate") or "See link for info",
                "url": f"https://lejeboligmaegleren.dk/cases/{case_id}/",
                "source": "Lejeboligmægleren",
                "transaction_type": "rent",
                "price_period": "month",
                "rooms": case.get("Rooms") or case.get("rooms"),
                "size_sqm": case.get("Size") or case.get("size"),
                "raw_text": restriction_text,
                "canonical_key": canonical_listing_key(address_line, "rent"),
                "source_priority": 20,
            }
            if not contains_restricted_eligibility(restriction_text) and listing_matches_policy(
                listing
            ):
                listings.append(listing)
        if len(cases) < page_size:
            break
    return SourceSnapshot(source="Lejeboligmægleren", listings=listings)


NORHJEM_URL = "https://norhjem.dk/for-boligsoegende/ledige-boliger/"
NORHJEM_API_URL = "https://norhjem.dk/api/searchrental"


def parse_norhjem_results(records, blocked_urls=frozenset()):
    if not isinstance(records, list):
        raise SourceContractError("Norhjem search API must return a JSON list")
    listings = []
    for record in records:
        if not isinstance(record, dict):
            continue
        href = str(record.get("url") or "").strip()
        if not href or href in blocked_urls:
            continue
        status_text = normalize_text(record.get("status"))
        if status_text == "ledig":
            status = "Available"
        elif status_text in {"reserveret", "udlejet"}:
            status = "Reserved"
        else:
            continue
        address = str(record.get("address") or "").strip()
        postcode = str(record.get("zipCode") or "").strip()
        city = str(record.get("city") or "").strip()
        full_address = f"{address}, {postcode} {city}".strip(", ")
        listing = {
            "id": f"norhjem:{href}",
            "status": status,
            "name": full_address,
            "price": {"amount": record.get("price")},
            "location": {"formatted": full_address},
            "availableFrom": record.get("moveInDate") or "See link for info",
            "url": urljoin(NORHJEM_URL, href),
            "source": "Norhjem",
            "transaction_type": "rent",
            "price_period": "month",
            "rooms": record.get("rooms"),
            "size_sqm": record.get("area"),
            "raw_text": " ".join(
                str(record.get(field) or "") for field in ("type", "leasePeriod", "description")
            ),
            "canonical_key": canonical_listing_key(full_address, "rent"),
            "source_priority": 20,
        }
        if listing_matches_policy(listing):
            listings.append(listing)
    return listings


def fetch_norhjem(post_form, fetch_text):
    base_payload = {"maxPrice": "18000", "sort": ""}
    records = post_form(NORHJEM_API_URL, base_payload)
    student_records = post_form(
        NORHJEM_API_URL,
        {**base_payload, "facilities": "Kun for studerende"},
    )
    if not isinstance(records, list) or not isinstance(student_records, list):
        raise SourceContractError("Norhjem search API must return a JSON list")
    blocked_urls = {
        str(record.get("url")) for record in student_records if isinstance(record, dict)
    }
    candidates = parse_norhjem_results(records, blocked_urls=blocked_urls)
    listings = []
    valid_details = 0
    for listing in candidates:
        try:
            detail_html = fetch_text(listing["url"])
            if not detail_html.strip():
                raise SourceContractError("empty detail response")
            valid_details += 1
        except Exception as exc:
            print(f"Norhjem detail {listing['id']} failed: {exc}")
            continue
        main = re.search(r"<main\b[^>]*>([\s\S]*?)</main\s*>", detail_html, re.IGNORECASE)
        detail_text = _plain_text(main.group(1) if main else detail_html)
        listing["raw_text"] = f"{listing.get('raw_text', '')} {detail_text}"
        if not contains_restricted_eligibility(listing["raw_text"]):
            listings.append(listing)
    if candidates and valid_details == 0:
        raise SourceContractError("No Norhjem detail matched the restriction-screening contract")
    return SourceSnapshot(source="Norhjem", listings=listings)
