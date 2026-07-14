import http.cookiejar
import json
import re
import urllib.request
from html import unescape
from urllib.parse import urljoin

from housing_policy import canonical_listing_key, listing_matches_policy, normalize_text
from housing_sources import SourceContractError, SourceSnapshot

FIND_URL = "https://www.findbolig.nu/da-dk/find"
SEARCH_URL = "https://www.findbolig.nu/api/search"
MUNICIPAL_COMPANY_ID = "73d07df9-6e80-4b79-da2d-08dbb5297ffe"
MUNICIPAL_COMPANY_NAME = "Københavns Ejendomme"


def _contains_municipal_marker(configuration):
    organizations = (
        configuration.get("membershipOrganizations") if isinstance(configuration, dict) else None
    )
    if not isinstance(organizations, list):
        return False
    return any(
        isinstance(organization, dict)
        and isinstance(organization.get("companies"), dict)
        and organization["companies"].get(MUNICIPAL_COMPANY_ID) == MUNICIPAL_COMPANY_NAME
        for organization in organizations
    )


def _validate_configuration(html):
    match = re.search(
        r'<script[^>]+id=["\']search-configuration["\'][^>]*>([\s\S]*?)</script\s*>',
        html,
        re.IGNORECASE,
    )
    if not match:
        raise SourceContractError("Findbolig search configuration is missing")
    configuration = json.loads(unescape(match.group(1)).strip())
    if not _contains_municipal_marker(configuration):
        raise SourceContractError("Findbolig municipal company marker is missing")


def _results(data):
    for key in ("results", "searchResults", "items"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, list):
            return value
    raise SourceContractError("Findbolig response has no recognized results list")


def _is_waitlisted(item):
    return normalize_text(item.get("applicationType")) == "waitinglist" or normalize_text(
        item.get("rentModel")
    ) == "waitinglist"


def _normalize_residence(item):
    """Normalize a live findbolig.nu `/api/search` residence record.

    The production response is a flat `Residence` record (verified against a
    live, read-only capture): the owning company is `propertyCompanyId`
    (not a nested `company` object), the detail path is `uri` built from
    `shortId` (not the GUID `id`), the current asking rent is `rent`, and
    waitlist-only stock is marked via `applicationType`/`rentModel` rather
    than a `status` field.
    """
    if normalize_text(item.get("$type") or item.get("type")) != "residence":
        return None
    if item.get("propertyCompanyId") != MUNICIPAL_COMPANY_ID:
        return None
    if item.get("membersOnly"):
        return None
    if _is_waitlisted(item):
        return None
    if normalize_text(item.get("residenceAdvertStatus")) != "published":
        return None

    short_id = item.get("shortId")
    uri = str(item.get("uri") or "")
    if not short_id or not re.search(rf"/residence/{re.escape(str(short_id))}(?:[/?#]|$)", uri):
        return None

    street = str(item.get("street") or "").strip()
    number = item.get("number")
    unit = ".".join(
        str(part) for part in (item.get("floor"), item.get("door")) if part not in (None, "")
    )
    house = " ".join(part for part in (street, str(number) if number not in (None, "") else "") if part)
    if unit:
        house = f"{house}, {unit}"
    postcode = str(item.get("postalCode") or "").strip()
    city = str(item.get("city") or "").strip()
    full_address = f"{house}, {postcode} {city}".strip(", ")

    listing = {
        "id": f"findbolig:{short_id}",
        "status": "Available",
        "name": full_address,
        "price": {"amount": item.get("rent")},
        "location": {"formatted": full_address},
        "availableFrom": item.get("availableFrom") or "See link for info",
        "url": urljoin(FIND_URL, uri),
        "source": "Findbolig - Københavns Ejendomme",
        "transaction_type": "rent",
        "price_period": "month",
        "canonical_key": canonical_listing_key(full_address, "rent"),
        "source_priority": 20,
    }
    return listing if listing_matches_policy(listing) else None


def fetch_findbolig(fetch_text, post_json, max_pages=20):
    _validate_configuration(fetch_text(FIND_URL))
    listings = []
    for page in range(max_pages):
        payload = {
            "pageSize": 100,
            "page": page,
            "orderBy": "Created",
            "orderDirection": "DESC",
            "facets": [],
            "filters": {"PropertyCompanyId": [MUNICIPAL_COMPANY_ID]},
            "mixedResults": False,
        }
        data = post_json(SEARCH_URL, payload)
        items = _results(data)
        if not items:
            break
        listings.extend(filter(None, (_normalize_residence(item) for item in items)))
        total = data.get("totalResults") or data.get("total")
        if isinstance(total, int) and (page + 1) * 100 >= total:
            break
    return SourceSnapshot(source="Findbolig", listings=listings)


def make_findbolig_transport(headers):
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

    def fetch_text(url):
        request = urllib.request.Request(url, headers={**headers, "Accept": "text/html"})
        with opener.open(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")

    def post_json(url, payload):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                **headers,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Referer": FIND_URL,
                "Origin": "https://www.findbolig.nu",
            },
        )
        with opener.open(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    return fetch_text, post_json
