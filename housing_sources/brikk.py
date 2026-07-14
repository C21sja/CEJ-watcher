import re
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit

from housing_policy import (
    canonical_listing_key,
    extract_amount,
    extract_postcode,
    listing_matches_policy,
    normalize_text,
)
from housing_sources import SourceContractError, SourceSnapshot

BASE_URL = "https://www.brikk.dk"
SEARCH_URL = (
    BASE_URL
    + "/boliger-til-salg/?locations=k%C3%B8benhavn&type%5B%5D=Andelsbolig"
    + "&price_min=&price_max=&size_min=&size_max=&ground_area_size_min=&ground_area_size_max="
    + "&rooms_min=&rooms_max=&udgift_min=&udgift_max=&energimaerke=&sorting_type="
)
SOLD_TERMS = ("solgt", "kobstilbud allerede accepteret", "købstilbud allerede accepteret")


def _text(value):
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(without_tags).replace("\xa0", " ")).strip()


def _slug(href):
    return urlsplit(href).path.strip("/").split("/")[-1]


def parse_brikk_page(html, current_page=1):
    if "properties-for-sale-property-list-item" not in html and "Ingen boliger" not in html:
        raise SourceContractError("Brikk response has neither cards nor an empty-state marker")
    listings = []
    cards = re.findall(
        r'<li\b(?P<attrs>[^>]*class=["\'][^"\']*properties-for-sale-property-list-item[^"\']*["\'][^>]*)>'
        r"(?P<body>[\s\S]*?)</li\s*>",
        html,
        re.IGNORECASE,
    )
    for attributes, body in cards:
        class_text = normalize_text(attributes)
        text = _text(body)
        normalized = normalize_text(text)
        if "property list item sold" in class_text or any(
            normalize_text(term) in normalized for term in SOLD_TERMS
        ):
            continue
        link = re.search(r'href=["\']([^"\']*/ejendom/[^"\']+/)["\']', body, re.IGNORECASE)
        address_match = re.search(
            r'class=["\'][^"\']*address[^"\']*["\'][^>]*>([\s\S]*?)</span\s*>', body, re.IGNORECASE
        )
        price_match = re.search(
            r'class=["\'][^"\']*price[^"\']*["\'][^>]*>([\s\S]*?)</span\s*>', body, re.IGNORECASE
        )
        if not link or not address_match or not price_match:
            continue
        href = unescape(link.group(1))
        postcode = extract_postcode(text)
        price = extract_amount(_text(price_match.group(1)))
        address = _text(address_match.group(1))
        listing = {
            "id": f"brikk:{_slug(href)}",
            "status": "Available",
            "name": address,
            "price": {"amount": price},
            "location": {"formatted": address},
            "availableFrom": "See link for info",
            "url": urljoin(BASE_URL, href),
            "source": "Brikk",
            "transaction_type": "cooperative_sale",
            "price_period": "total",
            "price_limit_inclusive": False,
            "canonical_key": canonical_listing_key(address, "cooperative_sale"),
            "source_priority": 30,
        }
        if postcode and listing_matches_policy(listing):
            listings.append(listing)
    later_pages = []
    for attributes in re.findall(r"<a\b([^>]*)>", html, re.IGNORECASE):
        href_match = re.search(r'href=["\']([^"\']*p_page=(\d+)[^"\']*)["\']', attributes, re.IGNORECASE)
        if href_match and int(href_match.group(2)) > current_page:
            later_pages.append((int(href_match.group(2)), unescape(href_match.group(1))))
    next_href = min(later_pages)[1] if later_pages else None
    return listings, next_href


def _primary_detail_text(html):
    main = re.search(r"<main\b[^>]*>([\s\S]*?)</main\s*>", html, re.IGNORECASE)
    if not main:
        raise SourceContractError("Brikk detail page has no primary main container")
    scope = main.group(1)
    status_blocks = re.findall(
        r'<(?:section|div|p|span)\b[^>]*class=["\'][^"\']*(?:property-status|sale-status)[^"\']*["\'][^>]*>'
        r"([\s\S]*?)</(?:section|div|p|span)>",
        scope,
        re.IGNORECASE,
    )
    if not status_blocks:
        raise SourceContractError("Brikk detail page has no captured primary status container")
    return _text(" ".join(status_blocks))


def fetch_brikk(fetch_text, max_pages=50):
    selected = {}
    visited = set()
    current_url = SEARCH_URL
    for _page in range(max_pages):
        if current_url in visited:
            raise SourceContractError("Brikk pagination loop detected")
        visited.add(current_url)
        parsed = urlsplit(current_url)
        query = parse_qs(parsed.query)
        if parsed.netloc != "www.brikk.dk" or query.get("type[]") != ["Andelsbolig"]:
            raise SourceContractError("Brikk pagination lost the Andelsbolig filter")
        current_page_number = int(query.get("p_page", ["1"])[0])
        listings, next_href = parse_brikk_page(fetch_text(current_url), current_page_number)
        for listing in listings:
            selected[listing["id"]] = listing
        if not next_href:
            break
        next_query = parse_qs(urlsplit(urljoin(current_url, next_href)).query)
        next_page = next_query.get("p_page", [None])[0]
        if not next_page or not str(next_page).isdigit():
            raise SourceContractError("Brikk pagination link has no numeric p_page")
        current_url = f"{SEARCH_URL}&p_page={int(next_page)}"
    else:
        raise SourceContractError("Brikk pagination exceeded max_pages")

    active = []
    for listing in selected.values():
        raw_detail_text = _primary_detail_text(fetch_text(listing["url"]))
        detail_text = normalize_text(raw_detail_text)
        if any(normalize_text(term) in detail_text for term in SOLD_TERMS):
            continue
        listing["raw_text"] = raw_detail_text
        if listing_matches_policy(listing):
            active.append(listing)
    return SourceSnapshot(source="Brikk", listings=active)
