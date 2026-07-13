import http.cookiejar
import json
import re
import urllib.request
from html import unescape
from urllib.parse import urljoin

from housing_policy import canonical_listing_key, listing_matches_policy, normalize_text
from housing_sources import SourceContractError, SourceSnapshot

FIND_URL = "https://www.findbolig.nu/da-dk/find"
SEARCH_URL = "https://www.findbolig.nu/search"
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


def _normalize_residence(item):
    company = item.get("company") or item.get("propertyCompany") or {}
    item_type = str(item.get("$type") or item.get("type") or "")
    company_id = company.get("id") if isinstance(company, dict) else None
    company_id = company_id or item.get("propertyCompanyId")
    if item_type.lower() != "residence" or company_id != MUNICIPAL_COMPANY_ID:
        return None
    residence_id = item.get("id")
    residence_path = str(item.get("url") or item.get("path") or item.get("detailUrl") or "")
    if not residence_id or not re.search(
        rf"/residence/{re.escape(str(residence_id))}(?:[/?#]|$)", residence_path
    ):
        return None
    address = str(item.get("address") or item.get("name") or "").strip()
    postcode = str(item.get("postalCode") or item.get("postcode") or "").strip()
    city = str(item.get("city") or "").strip()
    status = str(item.get("status") or "").strip()
    if normalize_text(status) not in {
        "available",
        "ledig",
        "under opsigelse",
        "reserved",
        "reserveret",
    }:
        return None
    listing = {
        "id": f"findbolig:{residence_id}",
        "status": (
            "Available"
            if normalize_text(status) in {"available", "ledig", "under opsigelse"}
            else "Reserved"
        ),
        "name": address,
        "price": {"amount": item.get("monthlyRent") or item.get("rent")},
        "location": {"formatted": f"{address}, {postcode} {city}".strip(", ")},
        "availableFrom": item.get("availableFrom") or "See link for info",
        "url": urljoin(FIND_URL, residence_path),
        "source": "Findbolig - Københavns Ejendomme",
        "transaction_type": "rent",
        "price_period": "month",
        "canonical_key": canonical_listing_key(f"{address}, {postcode} {city}", "rent"),
        "source_priority": 20,
        "raw_text": " ".join(
            str(item.get(field) or "")
            for field in (
                "description",
                "langToDescription",
                "onlyFor",
                "requirements",
                "eligibility",
                "housingType",
                "membershipRequirement",
            )
        ),
    }
    return listing if residence_id and listing_matches_policy(listing) else None


def fetch_findbolig(fetch_text, post_json, max_pages=20):
    _validate_configuration(fetch_text(FIND_URL))
    listings = []
    for page in range(max_pages):
        payload = {
            "pageSize": 100,
            "page": page,
            "orderBy": "Created",
            "orderDirection": "DESC",
            "facets": {},
            "filters": {"PropertyCompanyId": [MUNICIPAL_COMPANY_ID]},
            "mixedResults": False,
        }
        data = post_json(SEARCH_URL, payload)
        items = _results(data)
        if not items:
            break
        listings.extend(filter(None, (_normalize_residence(item) for item in items)))
        total = data.get("total") or data.get("totalResults")
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
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Referer": FIND_URL,
            },
        )
        with opener.open(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    return fetch_text, post_json
