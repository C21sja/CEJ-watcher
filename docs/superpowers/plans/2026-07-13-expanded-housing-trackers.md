# Expanded Copenhagen Housing Trackers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the approved Copenhagen rental, cooperative-sale, and application-readiness sources with exact area/price filtering, fail-closed verification, deduplication, and a compact first-run Discord digest.

**Architecture:** Move shared eligibility and identity rules into `housing_policy.py`, put new source adapters behind a small `housing_sources` package, and keep orchestration/Discord delivery in `watcher.py`. Every adapter returns a `SourceSnapshot`; a cadence-aware registry fetches snapshots independently, the baseline pipeline digests newly introduced sources once, and the existing state file remains backward compatible.

**Tech Stack:** Python 3.12 standard library (`dataclasses`, `html`, `html.parser`, `json`, `re`, `urllib`), `unittest`, GitHub Actions, Discord webhooks.

**Approved design:** `docs/superpowers/specs/2026-07-13-expanded-housing-trackers-design.md`

**Contract-review clarifications:** This implementation plan tightens three design details: CPH Homes is read only through pinned HTTPS pages rather than its plain-HTTP REST fallback; every Kobenhavn.dk origin present in the implementation-time scan is a release-gated verifier target; and a genuinely new future Kobenhavn.dk host produces one non-mention manual-review inspection event instead of being silent. These refinements preserve the approved scope while closing integrity and coverage gaps found during planning.

---

## File Map

### Create

- `housing_policy.py` — postcode, price, restriction, commercial-use, canonical-key, and deduplication rules.
- `housing_sources/__init__.py` — `SourceSnapshot`, `SourceSpec`, and `SourceContractError` definitions.
- `housing_sources/findbolig.py` — exact municipal Findbolig adapter.
- `housing_sources/brikk.py` — paginated Brikk cooperative-sale adapter.
- `housing_sources/kobenhavn_dk.py` — aggregator candidate parser and origin verification.
- `housing_sources/landlords.py` — Taurus, Lejeboligmægleren, and Norhjem adapters.
- `housing_sources/readiness.py` — RLE, CPH Homes, and Værnedamsvej readiness adapters.
- `test_housing_policy.py` — shared-policy tests.
- `test_source_models.py` — snapshot, identity, deduplication, and sale-format tests.
- `test_findbolig_source.py` — municipal owner/API tests.
- `test_marketplace_sources.py` — Brikk and Kobenhavn.dk tests.
- `test_landlord_sources.py` — Taurus, Lejeboligmægleren, and Norhjem tests.
- `test_readiness_sources.py` — RLE, CPH Homes, and Værnedamsvej tests.
- `test_alert_pipeline.py` — baseline digest and readiness notification tests.
- `test_source_scheduler.py` — cadence, isolation, and end-to-end orchestration tests.
- `tests/fixtures/findbolig/` — sanitized live configuration, exact request body, and successful search response used as the release contract.
- `tests/fixtures/kobenhavn_dk/` — timestamped origin manifest plus one bounded active/inactive fixture set per host present in the implementation-time scan.
- `docs/manual-contact-emails.md` — ready-to-send ØENS and Ejendomskontoret drafts plus eligibility caveats.
- `docs/latest-source-scan.md` — one-time accepted/rejected/manual-review inventory evidence from the live read-only probe.

### Modify

- `watcher.py:1-1314` — import shared policy/adapters, classify AKF, format sales, send digests/readiness alerts, and use the source registry.
- `test_watcher.py:9-464` — replace broad postcode assumptions and hard-coded fast/slow assertions with the approved behavior.
- `README.md:1-98` — document sources, exact areas, thresholds, alert types, and cadence settings.
- `.github/workflows/watcher.yml:32-51` — compile all modules and discover every test file.

## Execution Rules

- Work in the task order below; later tasks rely on interfaces introduced earlier.
- Do not contact Discord during tests or live parser probes.
- Use only read-only unauthenticated source endpoints.
- Preserve unrelated user changes in the worktree.
- Run the focused test first, then the complete suite before each commit.

### Task 1: Shared Housing Policy and Global Area Enforcement

**Files:**
- Create: `housing_policy.py`
- Create: `test_housing_policy.py`
- Modify: `watcher.py:149-182,316-364,577-625,714-727`
- Modify: `test_watcher.py:9-61,352-377`

- [ ] **Step 1: Confirm the pre-change baseline**

Run:

```powershell
python -m unittest test_watcher -v
```

Expected: `Ran 41 tests` and `OK`.

- [ ] **Step 2: Write failing shared-policy tests**

Create `test_housing_policy.py`:

```python
import unittest

from housing_policy import (
    canonical_listing_key,
    contains_commercial_use,
    contains_restricted_eligibility,
    extract_amount,
    extract_postcode,
    is_preferred_postcode,
    listing_matches_policy,
)


class HousingPolicyTests(unittest.TestCase):
    def test_accepts_every_agreed_postcode_group(self):
        accepted = [1000, 1499, 1500, 1799, 1800, 2000, 2100, 2150, 2200, 2300, 2400, 2450]
        self.assertTrue(all(is_preferred_postcode(code) for code in accepted))

    def test_rejects_outer_and_explicitly_excluded_postcodes(self):
        rejected = [999, 2001, 2050, 2500, 2605, 2700, 2720, 2770, 2900]
        self.assertTrue(all(not is_preferred_postcode(code) for code in rejected))

    def test_parses_danish_amounts_and_postcodes(self):
        self.assertEqual(17500, extract_amount("17.500,- kr."))
        self.assertEqual(2799999, extract_amount("2.799.999 kr."))
        self.assertEqual(2400, extract_postcode("Lærkevej 10, 2400 København NV"))

    def test_rent_is_inclusive_but_sale_and_kobenhavn_rent_are_strict(self):
        normal_rent = {
            "name": "Nørrebrogade 1",
            "location": {"formatted": "Nørrebrogade 1, 2200 København N"},
            "price": {"amount": 18000},
            "transaction_type": "rent",
        }
        strict_rent = dict(normal_rent, price_limit=15000, price_limit_inclusive=False)
        strict_rent["price"] = {"amount": 15000}
        sale = dict(normal_rent, transaction_type="cooperative_sale", price={"amount": 2800000})
        self.assertTrue(listing_matches_policy(normal_rent))
        self.assertFalse(listing_matches_policy(strict_rent))
        self.assertFalse(listing_matches_policy(sale))

    def test_rejects_unknown_price_and_missing_postcode(self):
        missing_price = {
            "name": "Unknown",
            "location": {"formatted": "Studiestræde, 1455 København K"},
            "price": {"amount": "Unknown"},
        }
        missing_postcode = {
            "name": "Unknown",
            "location": {"formatted": "Somewhere in Copenhagen"},
            "price": {"amount": 12000},
        }
        self.assertFalse(listing_matches_policy(missing_price))
        self.assertFalse(listing_matches_policy(missing_postcode))

    def test_detects_restricted_and_commercial_text(self):
        self.assertTrue(contains_restricted_eligibility("Kun for studerende"))
        self.assertTrue(contains_restricted_eligibility("Seniorbolig 65+"))
        self.assertTrue(contains_restricted_eligibility("Kræver medlemskab af en pensionsordning"))
        self.assertFalse(contains_restricted_eligibility("Intet medlemskab kræves"))
        self.assertTrue(contains_commercial_use("Erhvervslokale indrettet som kontor og butikslokale"))
        self.assertFalse(contains_commercial_use("Privat lejlighed med altan"))
        self.assertFalse(contains_commercial_use("Bolig tæt på butikker og restauranter"))

    def test_policy_rejects_restricted_and_commercial_listings(self):
        base = {
            "name": "Nørrebrogade 1",
            "location": {"formatted": "Nørrebrogade 1, 2200 København N"},
            "price": {"amount": 12000},
            "transaction_type": "rent",
        }
        self.assertFalse(listing_matches_policy(dict(base, description="Kun for studerende")))
        self.assertFalse(listing_matches_policy(dict(base, description="Erhvervslokale indrettet som kontor")))
        cooperative = dict(
            base,
            transaction_type="cooperative_sale",
            price={"amount": 2_000_000},
            description="Køber bliver medlem af andelsforeningen",
        )
        self.assertTrue(listing_matches_policy(cooperative))
        self.assertFalse(listing_matches_policy(dict(cooperative, description="Kun for seniorer")))

    def test_canonical_key_keeps_floor_and_transaction_type(self):
        rent_key = canonical_listing_key("Händelsvej 23, 2. th., 2450 København SV", "rent")
        sale_key = canonical_listing_key("Händelsvej 23 2 TH 2450 København SV", "cooperative_sale")
        self.assertNotEqual(rent_key, sale_key)
        self.assertIn("handelsvej 23 2 th 2450 kobenhavn sv", rent_key)


if __name__ == "__main__":
    unittest.main()
```

Update the location assertions in `test_watcher.py` so they explicitly reject Brønshøj, Vanløse, Valby, and Kastrup while accepting Frederiksberg and København NV.

- [ ] **Step 3: Run the policy tests and verify the intended failure**

Run:

```powershell
python -m unittest test_housing_policy -v
```

Expected: `ModuleNotFoundError: No module named 'housing_policy'`.

- [ ] **Step 4: Implement the shared policy**

Create `housing_policy.py` with these public definitions:

```python
import hashlib
import re
import unicodedata

GENERAL_RENT_MAX = 18_000
COOPERATIVE_SALE_MAX = 2_800_000
PREFERRED_POSTCODES = frozenset({2100, 2150, 2200, 2300, 2400, 2450})
RESTRICTED_TERMS = (
    "kun for studerende",
    "only for students",
    "studiebolig",
    "ungdomsbolig",
    "kun for unge",
    "seniorbolig",
    "kun for seniorer",
    "aeldrebolig",
    "kun for pensionister",
    "pensionskunde",
    "kun for pensionskunder",
    "pensionskunder har fortrinsret",
    "medlemskab kraeves",
    "krav om medlemskab",
    "kun for medlemmer",
    "for medlemmer af",
)
RESTRICTED_PATTERNS = (
    r"\b(?:minimum|min)\s*(?:alder)?\s*(?:55|60|65)\b",
    r"\b(?:55|60|65)\s*aar\b",
    r"\b(?:skal|forudsaetter)\s+(?:vaere\s+)?medlem\s+af\b",
    r"\b(?:kraever|krav\s+om)\s+medlemskab\b",
    r"\b(?:pensionsordning|pensionsselskab)\b.{0,40}\b(?:krav|fortrinsret|kun)\b",
)
NEGATED_RESTRICTION_PATTERNS = (
    r"\b(?:intet|ingen|ikke noget)\s+medlemskab\s+(?:er\s+)?(?:krav|kraeves)\b",
    r"\bmedlemskab\s+(?:er\s+)?ikke\s+(?:et\s+)?krav\b",
)
COMMERCIAL_PATTERNS = (
    r"\b(?:erhvervslejemal|erhvervslokale|kontorlokale|butikslokale|lagerlokale|restaurantlokale|kliniklokale)\b",
    r"\b(?:udlejes|anvendes|indrettet)\s+(?:til|som)\s+(?:erhverv|kontor|butik|lager|restaurant|klinik)\b",
    r"\b(?:type|kategori)\s+(?:erhverv|kontor|butik|lager|restaurant|klinik)\b",
)
_TRANSLATION = str.maketrans(
    {
        "æ": "ae",
        "ø": "o",
        "å": "a",
        "Ã¦": "ae",
        "Ã¸": "o",
        "Ã¥": "a",
    }
)


def normalize_text(value):
    text = str(value or "").strip().lower().translate(_TRANSLATION)
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = decomposed.encode("ascii", errors="ignore").decode("ascii")
    ascii_text = re.sub(r"[^a-z0-9]+", " ", ascii_text)
    return re.sub(r"\s+", " ", ascii_text).strip()


def extract_amount(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"\d[\d.\s,]*", str(value))
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    return int(digits) if digits else None


def extract_postcode(value):
    match = re.search(r"\b(\d{4})\b", str(value or ""))
    return int(match.group(1)) if match else None


def is_preferred_postcode(postcode):
    if postcode is None:
        return False
    return 1000 <= int(postcode) <= 2000 or int(postcode) in PREFERRED_POSTCODES


def contains_restricted_eligibility(value, allow_cooperative_membership=False):
    text = normalize_text(value)
    if allow_cooperative_membership:
        text = re.sub(r"\b(?:kober\s+)?(?:bliver|skal\s+vaere)?\s*medlem\s+af\s+(?:en\s+)?andelsforeningen?\b", " ", text)
    for pattern in NEGATED_RESTRICTION_PATTERNS:
        text = re.sub(pattern, " ", text)
    return any(term in text for term in RESTRICTED_TERMS) or any(
        re.search(pattern, text) for pattern in RESTRICTED_PATTERNS
    )


def contains_commercial_use(value):
    text = normalize_text(value)
    return any(re.search(pattern, text) for pattern in COMMERCIAL_PATTERNS)


def canonical_listing_key(address, transaction_type):
    return f"{normalize_text(transaction_type)}:{normalize_text(address)}"


def listing_matches_policy(listing):
    location = listing.get("location") or {}
    location_text = location.get("formatted", "") if isinstance(location, dict) else str(location)
    combined_location = f"{listing.get('name', '')} {location_text}"
    postcode = extract_postcode(combined_location)
    if not is_preferred_postcode(postcode):
        return False

    amount = extract_amount((listing.get("price") or {}).get("amount"))
    if amount is None or amount <= 0:
        return False

    transaction_type = listing.get("transaction_type", "rent")
    eligibility_text = " ".join(
        str(listing.get(field) or "")
        for field in ("name", "description", "eligibility", "onlyFor", "requirements", "raw_text")
    )
    if contains_restricted_eligibility(
        eligibility_text,
        allow_cooperative_membership=transaction_type == "cooperative_sale",
    ) or contains_commercial_use(eligibility_text):
        return False

    default_limit = COOPERATIVE_SALE_MAX if transaction_type == "cooperative_sale" else GENERAL_RENT_MAX
    limit = int(listing.get("price_limit", default_limit))
    inclusive = bool(listing.get("price_limit_inclusive", transaction_type == "rent"))
    return amount <= limit if inclusive else amount < limit
```

In `watcher.py`, explicitly import `canonical_listing_key`, `contains_restricted_eligibility`, `deduplicate_listings`, `extract_amount`, `extract_postcode`, `is_preferred_postcode`, `listing_matches_policy`, and `normalize_text`. Keep compatibility aliases for existing callers, make `matches_general_listing_filters()` delegate to `listing_matches_policy()`, and make `is_city_apartment_target_area()` use the shared postcode test. Remove the old `2700/2720/2770` allowances and keyword-only arbitrary-location acceptance.

- [ ] **Step 5: Run focused and complete tests**

Run:

```powershell
python -m unittest test_housing_policy test_watcher -v
```

Expected: all policy tests and the updated watcher tests pass.

- [ ] **Step 6: Commit the policy boundary**

```powershell
git add housing_policy.py test_housing_policy.py watcher.py test_watcher.py
git commit -m "Add shared Copenhagen housing policy"
```

### Task 2: Source Models, Deduplication, and Sale Formatting

**Files:**
- Create: `housing_sources/__init__.py`
- Create: `test_source_models.py`
- Modify: `housing_policy.py`
- Modify: `watcher.py:389-433`

- [ ] **Step 1: Write failing model, deduplication, and formatting tests**

Create `test_source_models.py`:

```python
import unittest

import watcher
from housing_policy import deduplicate_listings
from housing_sources import SourceSnapshot, SourceSpec


class SourceModelTests(unittest.TestCase):
    def test_snapshot_defaults_are_independent(self):
        first = SourceSnapshot(source="First")
        second = SourceSnapshot(source="Second")
        first.listings.append({"id": "one"})
        first.diagnostics.append({"reason": "fixture"})
        self.assertEqual([], second.listings)
        self.assertEqual([], second.diagnostics)

    def test_source_spec_records_cadence_and_baseline_source(self):
        spec = SourceSpec(name="Findbolig", cadence="fast", fetch=lambda: SourceSnapshot("Findbolig"))
        self.assertEqual("fast", spec.cadence)
        self.assertTrue(spec.baseline)

    def test_deduplication_prefers_verified_origin(self):
        aggregator = {
            "id": "kobenhavn:1",
            "canonical_key": "cooperative_sale:handelsvej 23 2 th 2450 kobenhavn sv",
            "source_priority": 40,
        }
        origin = {
            "id": "brikk:1",
            "canonical_key": aggregator["canonical_key"],
            "source_priority": 10,
        }
        self.assertEqual([origin], deduplicate_listings([aggregator, origin]))

    def test_sale_price_does_not_say_per_month(self):
        self.assertEqual("2.795.000 kr.", watcher.format_price_for_display(2795000, "total"))
        self.assertEqual("17.500 kr/month", watcher.format_price_for_display(17500, "month"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests and verify missing interfaces**

Run:

```powershell
python -m unittest test_source_models -v
```

Expected: import failures for `housing_sources`, `deduplicate_listings`, or the second formatting argument.

- [ ] **Step 3: Implement source models and deduplication**

Create `housing_sources/__init__.py`:

```python
from dataclasses import dataclass, field
from typing import Callable


class SourceContractError(Exception):
    """Raised when a source responds but no longer matches its known contract."""


@dataclass
class SourceSnapshot:
    source: str
    listings: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class SourceSpec:
    name: str
    cadence: str
    fetch: Callable[[], SourceSnapshot]
    baseline: bool = True
```

Add to `housing_policy.py`:

```python
def deduplicate_listings(listings):
    selected = {}
    order = []
    for listing in listings:
        key = listing.get("canonical_key") or f"id:{listing.get('id', '')}"
        if key not in selected:
            selected[key] = listing
            order.append(key)
            continue
        current = selected[key]
        if int(listing.get("source_priority", 100)) < int(current.get("source_priority", 100)):
            selected[key] = listing
    return [selected[key] for key in order]
```

Change `watcher.format_price_for_display` to accept `price_period` and use Danish thousands separators:

```python
def format_price_for_display(raw_price, price_period="month"):
    price_amount = extract_price_amount(raw_price)
    if price_amount is None:
        text = str(raw_price).strip()
        return text or "Unknown"
    formatted = f"{price_amount:,}".replace(",", ".")
    return f"{formatted} kr." if price_period == "total" else f"{formatted} kr/month"
```

Pass `listing.get("price_period", "month")` from `build_listing_fields()`.

- [ ] **Step 4: Run the focused tests**

Run:

```powershell
python -m unittest test_source_models test_watcher -v
```

Expected: all tests pass, including existing `Unknown` price behavior.

- [ ] **Step 5: Commit the normalized source contract**

```powershell
git add housing_policy.py housing_sources/__init__.py test_source_models.py watcher.py
git commit -m "Add normalized source snapshots and deduplication"
```

### Task 3: Findbolig Municipal Adapter

**Files:**
- Create: `housing_sources/findbolig.py`
- Create: `test_findbolig_source.py`

- [ ] **Step 1: Write failing owner, record-type, and pagination tests**

Create `test_findbolig_source.py`:

```python
import json
import unittest

from housing_sources.findbolig import MUNICIPAL_COMPANY_ID, fetch_findbolig


CONFIG_HTML = (
    '<script type="vue-model" id="search-configuration">'
    + json.dumps(
        {
            "membershipOrganizations": [
                {"companies": {MUNICIPAL_COMPANY_ID: "Københavns Ejendomme"}}
            ]
        }
    )
    + "</script>"
)
EXPECTED_FIRST_PAYLOAD = {
    "pageSize": 100,
    "page": 0,
    "orderBy": "Created",
    "orderDirection": "DESC",
    "facets": {},
    "filters": {"PropertyCompanyId": [MUNICIPAL_COMPANY_ID]},
    "mixedResults": False,
}


class FindboligSourceTests(unittest.TestCase):
    def test_keeps_only_exact_municipal_residences(self):
        pages = [
            {
                "results": [
                    {
                        "$type": "Residence",
                        "id": 288001,
                        "company": {"id": MUNICIPAL_COMPANY_ID, "name": "Københavns Ejendomme"},
                        "address": "Nørrebrogade 10, 2. tv.",
                        "postalCode": "2200",
                        "city": "København N",
                        "monthlyRent": 17500,
                        "status": "Available",
                        "url": "/da-dk/residence/288001",
                    },
                    {
                        "$type": "Residence",
                        "id": 288002,
                        "company": {"id": "private-company", "name": "Private"},
                        "address": "Nørrebrogade 12",
                        "postalCode": "2200",
                        "city": "København N",
                        "monthlyRent": 12000,
                        "url": "/da-dk/residence/288002",
                    },
                    {
                        "$type": "Property",
                        "id": 288003,
                        "company": {"id": MUNICIPAL_COMPANY_ID, "name": "Københavns Ejendomme"},
                        "address": "Waiting list",
                        "postalCode": "2200",
                        "monthlyRent": 10000,
                        "url": "/da-dk/property/288003",
                    },
                    {
                        "$type": "Residence",
                        "id": 288004,
                        "company": {"id": MUNICIPAL_COMPANY_ID, "name": "Københavns Ejendomme"},
                        "address": "Østerbrogade 20",
                        "postalCode": "2100",
                        "city": "København Ø",
                        "monthlyRent": 11000,
                        "status": "Available",
                        "description": "Kræver medlemskab af pensionsordning",
                        "url": "/da-dk/residence/288004",
                    },
                ],
                "total": 4,
            }
        ]
        payloads = []

        def post_json(_url, payload):
            payloads.append(payload)
            return pages.pop(0)

        snapshot = fetch_findbolig(lambda _url: CONFIG_HTML, post_json)
        self.assertEqual(["findbolig:288001"], [item["id"] for item in snapshot.listings])
        self.assertEqual(EXPECTED_FIRST_PAYLOAD, payloads[0])

    def test_stops_when_a_page_is_empty(self):
        calls = []

        def post_json(_url, payload):
            calls.append(payload["page"])
            return {"results": [], "total": 0}

        snapshot = fetch_findbolig(lambda _url: CONFIG_HTML, post_json)
        self.assertEqual([], snapshot.listings)
        self.assertEqual([0], calls)

    def test_missing_results_key_is_a_contract_error_not_an_empty_feed(self):
        with self.assertRaisesRegex(Exception, "results list"):
            fetch_findbolig(lambda _url: CONFIG_HTML, lambda _url, _payload: {"total": 0})

    def test_rejects_configuration_without_exact_owner(self):
        with self.assertRaisesRegex(Exception, "municipal company marker"):
            fetch_findbolig(lambda _url: '<script id="search-configuration">{}</script>', lambda _u, _p: {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests and verify the module is absent**

Run:

```powershell
python -m unittest test_findbolig_source -v
```

Expected: `ModuleNotFoundError: No module named 'housing_sources.findbolig'`.

- [ ] **Step 3: Implement configuration validation and municipal normalization**

Create `housing_sources/findbolig.py` with these interfaces and constants:

```python
import json
import re
import http.cookiejar
from html import unescape
from urllib.parse import urljoin
import urllib.request

from housing_policy import canonical_listing_key, listing_matches_policy, normalize_text
from housing_sources import SourceContractError, SourceSnapshot

FIND_URL = "https://www.findbolig.nu/da-dk/find"
SEARCH_URL = "https://www.findbolig.nu/search"
MUNICIPAL_COMPANY_ID = "73d07df9-6e80-4b79-da2d-08dbb5297ffe"
MUNICIPAL_COMPANY_NAME = "Københavns Ejendomme"


def _contains_municipal_marker(configuration):
    organizations = configuration.get("membershipOrganizations") if isinstance(configuration, dict) else None
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
        r'<script[^>]+id=["\']search-configuration["\'][^>]*>([\s\S]*?)</script>',
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
    if not residence_id or not re.search(rf"/residence/{re.escape(str(residence_id))}(?:[/?#]|$)", residence_path):
        return None
    address = str(item.get("address") or item.get("name") or "").strip()
    postcode = str(item.get("postalCode") or item.get("postcode") or "").strip()
    city = str(item.get("city") or "").strip()
    status = str(item.get("status") or "").strip()
    if normalize_text(status) not in {"available", "ledig", "under opsigelse", "reserved", "reserveret"}:
        return None
    listing = {
        "id": f"findbolig:{residence_id}",
        "status": "Available" if normalize_text(status) in {"available", "ledig", "under opsigelse"} else "Reserved",
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
            for field in ("description", "langToDescription", "onlyFor", "requirements", "eligibility", "housingType", "membershipRequirement")
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
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def fetch_text(url):
        request = urllib.request.Request(url, headers={**headers, "Accept": "text/html"})
        with opener.open(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")

    def post_json(url, payload):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={**headers, "Accept": "application/json", "Content-Type": "application/json", "Referer": FIND_URL},
        )
        with opener.open(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    return fetch_text, post_json
```

The production registry must create both closures from one `make_findbolig_transport(HEADERS)` call for each poll so the configuration GET and search POST share cookies. Before writing the final fixture assertions, capture one successful, read-only, same-session browser request and response into `tests/fixtures/findbolig/`: sanitized configuration HTML, the complete JSON request body, and the complete successful response. Replace `EXPECTED_FIRST_PAYLOAD` and the response fixture wholesale from that capture; assert deep equality for the entire body, including the object-shaped `facets` value, rather than checking a few keys. Then run the same request through `make_findbolig_transport()` and prove its result fields normalize correctly.

This is a release gate, not an optional probe: Findbolig must not be enabled in the registry or marked baseline-complete until the transport reproduces a successful response and all captured request/response fields agree with the fixture. If the same-origin POST cannot be reproduced, raise `SourceContractError`, keep the source visibly unbaselined, and document the failed contract in `docs/latest-source-scan.md`; never reinterpret a 404, missing key, or unsuccessful probe as zero municipal homes.

- [ ] **Step 4: Run focused tests and the complete suite**

Run:

```powershell
python -m unittest test_findbolig_source -v
python -m unittest discover -v
```

Expected: both commands pass.

- [ ] **Step 5: Commit the Findbolig adapter**

```powershell
git add housing_sources/findbolig.py test_findbolig_source.py
git commit -m "Add municipal Findbolig source adapter"
```

### Task 4: Brikk Cooperative-Sale Adapter

**Files:**
- Create: `housing_sources/brikk.py`
- Create: `test_marketplace_sources.py`

- [ ] **Step 1: Write failing active/sold, threshold, identity, and pagination tests**

Create `test_marketplace_sources.py`:

```python
import unittest

from housing_sources.brikk import fetch_brikk, parse_brikk_page


BRIKK_PAGE = """
<ul>
  <li class="properties-for-sale-property-list-item">
    <a href="https://www.brikk.dk/ejendom/handelsvej-23-2-th/">
      <span class="properties-for-sale-property-list-item-address">Händelsvej 23, 2. th., 2450 København SV</span>
      <span class="properties-for-sale-property-list-item-price">1.848.000 kr.</span>
    </a>
  </li>
  <li class="properties-for-sale-property-list-item properties-for-sale-property-list-item-sold">
    <a href="https://www.brikk.dk/ejendom/sommerstedgade-9b-3-th/">
      <span class="properties-for-sale-property-list-item-address">Sommerstedgade 9B, 3. th., 1718 København V</span>
      <span class="properties-for-sale-property-list-item-price">2.299.000 kr.</span><span>SOLGT</span>
    </a>
  </li>
  <li class="properties-for-sale-property-list-item">
    <a href="/ejendom/store-kongensgade-42a-2/">
      <span class="properties-for-sale-property-list-item-address">Store Kongensgade 42A, 2., 1264 København K</span>
      <span class="properties-for-sale-property-list-item-price">2.819.581 kr.</span>
    </a>
  </li>
</ul>
<a class="page-numbers" href="?p_page=2">2</a>
"""


class BrikkSourceTests(unittest.TestCase):
    def test_keeps_only_active_andels_below_limit(self):
        listings, next_href = parse_brikk_page(BRIKK_PAGE)
        self.assertEqual(["brikk:handelsvej-23-2-th"], [item["id"] for item in listings])
        self.assertEqual("total", listings[0]["price_period"])
        self.assertIn("p_page=2", next_href)

    def test_adjacent_sold_card_does_not_contaminate_active_card(self):
        listings, _next = parse_brikk_page(BRIKK_PAGE)
        self.assertEqual(["Händelsvej 23, 2. th., 2450 København SV"], [item["name"] for item in listings])

    def test_fetches_each_page_once_and_deduplicates_links(self):
        calls = []

        def fetch_text(url):
            calls.append(url)
            if "/ejendom/" in url:
                return '<main><section class="property-status"><h1>Händelsvej 23, 2. th.</h1><p>Aktiv andelsbolig</p></section></main>'
            return BRIKK_PAGE if "p_page=2" not in url else BRIKK_PAGE.split('<a class="page-numbers"')[0]

        snapshot = fetch_brikk(fetch_text, max_pages=5)
        search_calls = [url for url in calls if "/boliger-til-salg/" in url]
        self.assertEqual(2, len(search_calls))
        self.assertTrue(all("type%5B%5D=Andelsbolig" in url for url in search_calls))
        self.assertEqual(1, len(snapshot.listings))

    def test_detail_with_accepted_offer_is_rejected(self):
        snapshot = fetch_brikk(
            lambda url: BRIKK_PAGE.split('<a class="page-numbers"')[0]
            if "/boliger-til-salg/" in url
            else '<main><section class="property-status">Købstilbud allerede accepteret</section></main>'
        )
        self.assertEqual([], snapshot.listings)

    def test_footer_or_recommended_sold_copy_does_not_reject_active_detail(self):
        def fetch_text(url):
            if "/boliger-til-salg/" in url:
                return BRIKK_PAGE.split('<a class="page-numbers"')[0]
            return """
            <main><section class="property-status"><h1>Händelsvej 23</h1><p>Aktiv andelsbolig</p></section></main>
            <footer>Se også vores senest solgte boliger</footer>
            """

        self.assertEqual(["brikk:handelsvej-23-2-th"], [item["id"] for item in fetch_brikk(fetch_text).listings])

    def test_unrecognized_detail_status_container_fails_closed(self):
        with self.assertRaisesRegex(Exception, "primary status container"):
            fetch_brikk(
                lambda url: BRIKK_PAGE.split('<a class="page-numbers"')[0]
                if "/boliger-til-salg/" in url
                else "<main><p>Aktiv bolig</p><aside>Andre boliger er solgt</aside></main>"
            )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the focused test and verify the module is absent**

Run:

```powershell
python -m unittest test_marketplace_sources.BrikkSourceTests -v
```

Expected: `ModuleNotFoundError: No module named 'housing_sources.brikk'`.

- [ ] **Step 3: Implement bounded page parsing and sale normalization**

Create `housing_sources/brikk.py`:

```python
import re
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit

from housing_policy import canonical_listing_key, extract_amount, extract_postcode, listing_matches_policy, normalize_text
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
        r'<li\b(?P<attrs>[^>]*class=["\'][^"\']*properties-for-sale-property-list-item[^"\']*["\'][^>]*)>(?P<body>[\s\S]*?)</li>',
        html,
        re.IGNORECASE,
    )
    for attributes, body in cards:
        class_text = normalize_text(attributes)
        text = _text(body)
        normalized = normalize_text(text)
        if "property list item sold" in class_text or any(normalize_text(term) in normalized for term in SOLD_TERMS):
            continue
        link = re.search(r'href=["\']([^"\']*/ejendom/[^"\']+/)["\']', body, re.IGNORECASE)
        address_match = re.search(r'class=["\'][^"\']*address[^"\']*["\'][^>]*>([\s\S]*?)</span>', body, re.IGNORECASE)
        price_match = re.search(r'class=["\'][^"\']*price[^"\']*["\'][^>]*>([\s\S]*?)</span>', body, re.IGNORECASE)
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
    main = re.search(r"<main\b[^>]*>([\s\S]*?)</main>", html, re.IGNORECASE)
    if not main:
        raise SourceContractError("Brikk detail page has no primary main container")
    scope = main.group(1)
    status_blocks = re.findall(
        r'<(?:section|div|p|span)\b[^>]*class=["\'][^"\']*(?:property-status|sale-status)[^"\']*["\'][^>]*>([\s\S]*?)</(?:section|div|p|span)>',
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
```

Before committing, save a sanitized pair of consecutive live result pages as test fixtures and replace the illustrative page-number markup with the observed numbered/next links. The focused test must prove that the adapter follows the actual `p_page` hrefs without relying on English `next` text, preserves the original `locations` and `type[]=Andelsbolig` filters, and never follows a backward page. Likewise, capture the live primary detail/status container and keep `_primary_detail_text()` scoped to it; footer, navigation, and recommended sold homes must not affect active-state verification.

- [ ] **Step 4: Run focused and complete tests**

Run:

```powershell
python -m unittest test_marketplace_sources.BrikkSourceTests -v
python -m unittest discover -v
```

Expected: both commands pass.

- [ ] **Step 5: Commit the Brikk adapter**

```powershell
git add housing_sources/brikk.py test_marketplace_sources.py
git commit -m "Add Brikk cooperative housing adapter"
```

### Task 5: Kobenhavn.dk Discovery and Origin Verification

**Files:**
- Create: `housing_sources/kobenhavn_dk.py`
- Create: `tests/fixtures/kobenhavn_dk/origin_manifest.json`
- Create: `tests/fixtures/kobenhavn_dk/<captured-host-and-record>.html` for every implementation-time origin host
- Modify: `test_marketplace_sources.py`

- [ ] **Step 1: Add failing parsing and fail-closed verification tests**

Append to `test_marketplace_sources.py`:

```python
import json
from pathlib import Path

from housing_sources.kobenhavn_dk import ORIGIN_VERIFIERS, _akutbolig_inventory_url, fetch_kobenhavn, parse_candidates, verify_candidate


KOBENHAVN_PAGE = """
<h4>Lejligheder til leje, København og omegn.</h4>
<table><tr><td><a href="https://www.akutbolig.dk/vis/486255">H.C. Andersens Boulevard 10, 2. th., 1553 København V</a></td><td>4500</td><td>1</td><td>20</td></tr></table>
<h4>2450 København SV - Andelsbolig</h4>
<table><tr><td><a href="https://broker.example/handelsvej-23">Händelsvej 23, 2. th., 2450 København SV</a></td><td>1.848.000</td><td>3</td><td>74</td></tr></table>
"""


class KobenhavnSourceTests(unittest.TestCase):
    def test_parses_rental_and_cooperative_candidates_with_strict_limits(self):
        candidates = parse_candidates(KOBENHAVN_PAGE)
        self.assertEqual({"rent", "cooperative_sale"}, {item["transaction_type"] for item in candidates})
        rent = next(item for item in candidates if item["transaction_type"] == "rent")
        self.assertEqual(15000, rent["price_limit"])
        self.assertFalse(rent["price_limit_inclusive"])

    def test_rejects_stale_detail_and_accepts_current_inventory_membership(self):
        candidate = parse_candidates(KOBENHAVN_PAGE)[0]
        stale = verify_candidate(candidate, lambda _url: "Denne bolig er ikke længere aktiv")
        self.assertIsNone(stale)

        def active_fetch(url):
            if url.endswith("/koebenhavn-v"):
                return '<a href="/vis/486255">H.C. Andersens Boulevard 10, 2. th.</a><span>4.750 kr.</span><span>1 værelse</span><span>20 m²</span>'
            return ""

        active = verify_candidate(candidate, active_fetch)
        self.assertEqual("kobenhavn:rent:akutbolig.dk:486255", active["id"])
        self.assertEqual(4750, active["price"]["amount"])

    def test_unsupported_origin_is_not_fetched(self):
        candidate = parse_candidates(KOBENHAVN_PAGE)[1]
        self.assertIsNone(verify_candidate(candidate, lambda _url: "Til salg"))

    def test_captured_origin_manifest_has_a_verifier_for_every_current_host(self):
        manifest_path = Path("tests/fixtures/kobenhavn_dk/origin_manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(manifest["captured_at"])
        self.assertEqual(set(manifest["origin_hosts"]), set(ORIGIN_VERIFIERS))

    def test_private_or_non_https_origin_is_never_fetched(self):
        candidate = dict(parse_candidates(KOBENHAVN_PAGE)[1], origin_url="http://127.0.0.1/listing", origin_host="127.0.0.1")
        calls = []
        self.assertIsNone(verify_candidate(candidate, lambda url: calls.append(url) or ""))
        self.assertEqual([], calls)

    def test_frederiksberg_uses_verified_inventory_route(self):
        self.assertEqual("https://www.akutbolig.dk/frederiksberg/lejlighed", _akutbolig_inventory_url(1900))
        self.assertEqual("https://www.akutbolig.dk/frederiksberg/lejlighed", _akutbolig_inventory_url(2000))

    def test_fetch_returns_only_origin_verified_rows(self):
        def fetch_text(url):
            if url == "https://www.kobenhavn.dk/bolig":
                return KOBENHAVN_PAGE
            if url.endswith("/koebenhavn-v"):
                return '<a href="/vis/486255">H.C. Andersens Boulevard 10, 2. th.</a><span>4.500 kr.</span><span>1 værelse</span><span>20 m²</span>'
            return ""

        snapshot = fetch_kobenhavn(fetch_text)
        self.assertEqual(["kobenhavn:rent:akutbolig.dk:486255"], [item["id"] for item in snapshot.listings])
        self.assertEqual("manual_review", snapshot.diagnostics[0]["outcome"])
        self.assertIn("broker.example", snapshot.diagnostics[0]["origin_url"])
        self.assertEqual("inspection", snapshot.events[0]["kind"])
        self.assertFalse(snapshot.events[0]["urgent"])
```

Before creating `origin_manifest.json`, perform the requested read-only one-time scan of the live Kobenhavn.dk page and record every distinct HTTPS origin host represented by an in-area rental below 15,000 kr. or cooperative sale below 2.8 million kr. For each host, capture a bounded active record, an inactive/stale record where available, the exact current-status container or inventory membership, the labelled base rent/asking price, and the unit identity fields. Do not put query tokens, cookies, personal data, or unrelated page content in fixtures. A host may enter `ORIGIN_VERIFIERS` only after its captured tests prove all four facts.

The manifest equality test is a release gate: Task 5 is incomplete and Kobenhavn.dk stays disabled in the production registry until every host in that timestamped scan has a host-specific verifier. An unknown host discovered on a later poll remains unfetched, but must generate the stable manual-review inspection event below so it is visible in the compact Discord digest and subsequent change alerts instead of disappearing silently.

- [ ] **Step 2: Run the new test class and verify the missing module**

Run:

```powershell
python -m unittest test_marketplace_sources.KobenhavnSourceTests -v
```

Expected: `ModuleNotFoundError: No module named 'housing_sources.kobenhavn_dk'`.

- [ ] **Step 3: Implement row parsing and explicit origin verifiers**

Create `housing_sources/kobenhavn_dk.py`:

```python
import hashlib
import re
from html import unescape
from urllib.parse import urlparse

from housing_policy import canonical_listing_key, extract_amount, extract_postcode, listing_matches_policy, normalize_text
from housing_sources import SourceContractError, SourceSnapshot

PAGE_URL = "https://www.kobenhavn.dk/bolig"
NEGATIVE_MARKERS = ("ikke til salg", "solgt", "udlejet", "ikke laengere aktiv", "fjernet")


def _text(value):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _rows(section):
    return re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", section, re.IGNORECASE)


def _candidate_from_row(row, transaction_type):
    link = re.search(r'href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', row, re.IGNORECASE)
    if not link:
        return None
    cells = [_text(cell) for cell in re.findall(r"<td[^>]*>([\s\S]*?)</td>", row, re.IGNORECASE)]
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
    headings = list(re.finditer(r"<h4[^>]*>([\s\S]*?)</h4>", html, re.IGNORECASE))
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
        candidates.extend(filter(None, (_candidate_from_row(row, transaction_type) for row in _rows(section))))
    return candidates


def _akutbolig_inventory_url(postcode):
    if 1000 <= postcode <= 1499:
        return "https://www.akutbolig.dk/koebenhavn-k/lejlighed"
    if 1500 <= postcode <= 1799:
        return "https://www.akutbolig.dk/koebenhavn-v"
    if 1800 <= postcode <= 2000:
        return "https://www.akutbolig.dk/frederiksberg/lejlighed"
    slug = {2100: "koebenhavn-oe", 2150: "nordhavn", 2200: "koebenhavn-n", 2300: "koebenhavn-s", 2400: "koebenhavn-nv", 2450: "koebenhavn-sv"}.get(postcode)
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
    rooms_match = rooms is None or re.search(rf"\b{int(rooms)}\s*(?:vaer(?:else|elser)?|rum)\b", normalized_record)
    size_match = size is None or re.search(rf"\b{int(size)}\s*m(?:2|²)\b", normalized_record)
    return bool(rooms_match and size_match)


def _current_origin_price(record, transaction_type):
    amounts = [
        extract_amount(value)
        for value in re.findall(r"\b\d{1,3}(?:[.\s]\d{3})+\s*(?:kr\.?|,-)|\b\d{4,7}\s*(?:kr\.?|,-)", _text(record), re.IGNORECASE)
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
    tolerance = max(1_000, int(candidate["price"] * 0.15)) if candidate["transaction_type"] == "rent" else max(50_000, int(candidate["price"] * 0.05))
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
            diagnostics.append({"candidate_id": candidate["candidate_id"], "origin_url": candidate["origin_url"], "outcome": "error", "reason": str(exc)})
            continue
        if verified:
            listings.append(verified)
        else:
            diagnostics.append({"candidate_id": candidate["candidate_id"], "origin_url": candidate["origin_url"], "outcome": "suppressed", "reason": "unsupported, inactive, or ambiguous origin"})
    review_items = [item for item in diagnostics if item["outcome"] == "manual_review"]
    events = []
    if review_items:
        signals = sorted(f"{urlparse(item['origin_url']).netloc.lower()}:{item['candidate_id']}" for item in review_items)
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
```

Akutbolig is the worked example and is checked through exact membership in its current inventory, including its verified Frederiksberg route. Add the other implementation-time manifest hosts to `ORIGIN_VERIFIERS` with equally bounded extractors before enabling the source. Non-HTTPS URLs, IP/private hosts, and unknown origins are never fetched. Unknown hosts create review diagnostics/events; known-host records that are proven inactive or fail identity/price checks remain suppressed. Each candidate failure is isolated so one broken origin cannot abort the whole Kobenhavn.dk source.

- [ ] **Step 4: Run focused and complete tests**

Run:

```powershell
python -m unittest test_marketplace_sources.KobenhavnSourceTests -v
python -m unittest discover -v
```

Expected: all tests pass; every captured current host has a verifier, unknown future hosts create one non-mention review event, and Akutbolig uses its current inventory price.

- [ ] **Step 5: Commit the fail-closed discovery adapter**

```powershell
git add housing_sources/kobenhavn_dk.py test_marketplace_sources.py
git commit -m "Add verified Kobenhavn.dk discovery source"
```

### Task 6: Taurus Adapter

**Files:**
- Create: `housing_sources/landlords.py`
- Create: `test_landlord_sources.py`

- [ ] **Step 1: Write failing Taurus parsing tests**

Create `test_landlord_sources.py`:

```python
import unittest

from housing_sources.landlords import fetch_taurus, parse_taurus_overview


TAURUS_HTML = """
<a class="rental-item" href="/boligudlejning/lejebolig?id=101" data-cities="koebenhavn-n" data-rooms="2-vaer" data-living-area="76" data-price="17500">
  <h3>Centralt på Nørrebro</h3><p>Lejlighed - 2 vær. - Nørrebrogade, København N</p>
</a>
<a class="rental-item" href="/boligudlejning/lejebolig?id=102" data-cities="koebenhavn-s" data-rooms="4-vaer" data-living-area="115" data-price="20500">
  <h3>Ny bolig på Amager</h3>
</a>
<a class="rental-item" href="/boligudlejning/lejebolig?id=103" data-cities="valby" data-rooms="2-vaer" data-living-area="60" data-price="12000">
  <h3>Bolig i Valby</h3>
</a>
<a class="rental-item" href="/boligudlejning/lejebolig?id=104" data-cities="koebenhavn-s" data-rooms="2-vaer" data-living-area="65" data-price="15000">
  <h3>Reserveret på Amager</h3>
</a>
<a class="rental-item" href="/boligudlejning/lejebolig?id=105" data-cities="koebenhavn-n" data-rooms="1-vaer" data-living-area="35" data-price="8000">
  <h3>Studiebolig</h3>
</a>
"""

TAURUS_DETAILS = {
    "101": "<main><strong>Status:</strong> ledig <strong>Husleje:</strong> 17.500 kr. <strong>Vejnavn:</strong> Nørrebrogade <strong>Husnummer:</strong> 190, 5. <strong>Postnummer:</strong> 2200 <strong>By:</strong> København N <strong>Studiebolig:</strong> Nej</main>",
    "103": "<main><strong>Status:</strong> ledig <strong>Husleje:</strong> 12.000 kr. <strong>Vejnavn:</strong> Valby Langgade <strong>Husnummer:</strong> 1 <strong>Postnummer:</strong> 2500 <strong>By:</strong> Valby <strong>Studiebolig:</strong> Nej</main>",
    "104": "<main><strong>Status:</strong> reserveret <strong>Husleje:</strong> 15.000 kr. <strong>Vejnavn:</strong> Amagerbrogade <strong>Husnummer:</strong> 4 <strong>Postnummer:</strong> 2300 <strong>By:</strong> København S <strong>Studiebolig:</strong> Nej</main>",
    "105": "<main><strong>Status:</strong> ledig <strong>Husleje:</strong> 8.000 kr. <strong>Vejnavn:</strong> Nørrebrogade <strong>Husnummer:</strong> 5 <strong>Postnummer:</strong> 2200 <strong>By:</strong> København N <strong>Studiebolig:</strong> Ja</main>",
}


class TaurusSourceTests(unittest.TestCase):
    def test_keeps_only_active_in_area_below_limit(self):
        candidates = parse_taurus_overview(TAURUS_HTML)
        self.assertEqual(["101", "102", "103", "104", "105"], [item["record_id"] for item in candidates])

    def test_fetch_uses_the_official_vacancy_page(self):
        calls = []
        def fetch_text(url):
            calls.append(url)
            if url.endswith("ledige-lejemal/"):
                return TAURUS_HTML
            return TAURUS_DETAILS[url.split("id=")[-1]]

        snapshot = fetch_taurus(fetch_text)
        self.assertEqual(["taurus:101", "taurus:104"], [item["id"] for item in snapshot.listings])
        self.assertEqual(["Available", "Reserved"], [item["status"] for item in snapshot.listings])
        self.assertEqual(76, snapshot.listings[0]["size_sqm"])
        self.assertEqual(2, snapshot.listings[0]["rooms"])
        self.assertFalse(any("id=102" in url for url in calls))
        self.assertEqual("Taurus", snapshot.source)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the Taurus tests and verify the module is absent**

Run:

```powershell
python -m unittest test_landlord_sources.TaurusSourceTests -v
```

Expected: `ModuleNotFoundError: No module named 'housing_sources.landlords'`.

- [ ] **Step 3: Implement the Taurus parser and fetcher**

Create `housing_sources/landlords.py` with the Taurus portion:

```python
import re
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit

from housing_policy import canonical_listing_key, contains_restricted_eligibility, extract_amount, listing_matches_policy, normalize_text
from housing_sources import SourceContractError, SourceSnapshot

TAURUS_URL = "https://www.taurus.dk/boligudlejning/ledige-lejemal/"


def _plain_text(value):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _attribute(attributes, name):
    match = re.search(rf'{name}=["\']([^"\']*)["\']', attributes, re.IGNORECASE)
    return unescape(match.group(1)) if match else ""


def parse_taurus_overview(html):
    if "rental-item" not in html and "ingen ledige" not in normalize_text(html):
        raise SourceContractError("Taurus rental cards are missing")
    candidates = []
    pattern = re.compile(
        r'<a(?P<attrs>[^>]*class=["\'][^"\']*rental-item[^"\']*["\'][^>]*)>(?P<body>[\s\S]*?)</a>',
        re.IGNORECASE,
    )
    for card in pattern.finditer(html):
        attrs = card.group("attrs")
        body = card.group("body")
        href = _attribute(attrs, "href")
        record_id = parse_qs(urlsplit(href).query).get("id", [""])[0]
        price = extract_amount(_attribute(attrs, "data-price"))
        if record_id and price:
            candidates.append(
                {
                    "record_id": record_id,
                    "url": urljoin(TAURUS_URL, href),
                    "price": price,
                    "rooms": extract_amount(_attribute(attrs, "data-rooms")),
                    "size_sqm": extract_amount(_attribute(attrs, "data-living-area")),
                }
            )
    return candidates


def _label_value(html, label):
    match = re.search(
        rf">\s*{re.escape(label)}\s*:?\s*</[^>]+>\s*(?:<[^>]+>\s*)?([^<]+)",
        html,
        re.IGNORECASE,
    )
    return _plain_text(match.group(1)) if match else ""


def parse_taurus_detail(candidate, html):
    status_text = _label_value(html, "Status")
    street = _label_value(html, "Vejnavn")
    house_number = _label_value(html, "Husnummer")
    postcode = _label_value(html, "Postnummer")
    city = _label_value(html, "By")
    current_rent = extract_amount(_label_value(html, "Husleje"))
    if not all((status_text, street, house_number, postcode, current_rent)):
        raise SourceContractError(f"Taurus detail {candidate['record_id']} is missing labelled fields")
    normalized_status = normalize_text(status_text)
    if normalized_status == "ledig":
        status = "Available"
    elif normalized_status in {"reserveret", "udlejet", "under kontrakt"}:
        status = "Reserved"
    else:
        return None
    study_value = normalize_text(_label_value(html, "Studiebolig"))
    restriction_text = "Studiebolig" if study_value and study_value not in {"nej", "no", "false", "0"} else ""
    restriction_text += " " + _label_value(html, "Målgruppe") + " " + _label_value(html, "Krav")
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
    return listing if not contains_restricted_eligibility(restriction_text) and listing_matches_policy(listing) else None


def fetch_taurus(fetch_text):
    candidates = parse_taurus_overview(fetch_text(TAURUS_URL))
    shortlisted = [candidate for candidate in candidates if 0 < candidate["price"] <= 18_000]
    listings = []
    valid_details = 0
    for candidate in shortlisted:
        try:
            listing = parse_taurus_detail(candidate, fetch_text(candidate["url"]))
            valid_details += 1
        except Exception as exc:
            print(f"Taurus detail {candidate['record_id']} failed: {exc}")
            continue
        if listing:
            listings.append(listing)
    if shortlisted and valid_details == 0:
        raise SourceContractError("No shortlisted Taurus detail matched the labelled-field contract")
    return SourceSnapshot(source="Taurus", listings=listings)
```

- [ ] **Step 4: Run focused and complete tests**

Run:

```powershell
python -m unittest test_landlord_sources.TaurusSourceTests -v
python -m unittest discover -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the Taurus adapter**

```powershell
git add housing_sources/landlords.py test_landlord_sources.py
git commit -m "Add Taurus rental source"
```

### Task 7: Lejeboligmægleren Adapter

**Files:**
- Modify: `housing_sources/landlords.py`
- Modify: `test_landlord_sources.py`

- [ ] **Step 1: Add failing pagination and state tests**

Append to `test_landlord_sources.py`:

```python
from housing_sources.landlords import fetch_lejeboligmaegleren


class LejeboligmaeglerenSourceTests(unittest.TestCase):
    def test_paginates_until_empty_and_maps_actionable_states(self):
        calls = []
        pages_payloads = []
        pages = {
            1: {
                "Cases": [
                    {"Id": 701, "Address": "Sluseholmen 1", "City": {"ZipCode": 2450, "Name": "København SV"}, "Rent": 9200, "State": "Ledig", "Rooms": 2, "Size": 61, "AcquisitionDate": "2026-08-01", "Description": "Familiebolig", "Tags": [], "UnitType": "Lejlighed"},
                    {"Id": 702, "Address": "Amagerbrogade 2", "City": {"ZipCode": 2300, "Name": "København S"}, "Rent": 18000, "State": "Under opsigelse", "Rooms": 3, "Size": 78, "AcquisitionDate": "2026-09-01", "Description": "", "Tags": []},
                    {"Id": 703, "Address": "Amagerbrogade 4", "City": {"ZipCode": 2300, "Name": "København S"}, "Rent": 15000, "State": "Kontrakt under udarbejdelse", "Rooms": 2, "Size": 65, "AcquisitionDate": "2026-10-01", "Description": "", "Tags": []},
                    {"Id": 704, "Address": "Amagerbrogade 6", "City": {"ZipCode": 2300, "Name": "København S"}, "Rent": 8000, "State": "Ledig", "Rooms": 1, "Size": 30, "Description": "", "Tags": [99], "UnitType": 9},
                ],
                "HasMorePages": True,
            },
            2: {"Cases": [], "HasMorePages": True},
        }

        def post_json(_url, payload):
            calls.append(payload["PageIndex"])
            pages_payloads.append(payload)
            return pages[payload["PageIndex"]]

        def fetch_json(url):
            return [{"Id": 9, "Name": "Studiebolig"}] if url.endswith("UnitCaseTypes") else [{"Id": 99, "Name": "Kun for studerende"}]

        snapshot = fetch_lejeboligmaegleren(post_json, fetch_json, page_size=4)
        self.assertEqual([1, 2], calls)
        self.assertEqual(["Available", "Available", "Reserved"], [item["status"] for item in snapshot.listings])
        self.assertEqual("https://lejeboligmaegleren.dk/cases/701/", snapshot.listings[0]["url"])
        self.assertEqual(61, snapshot.listings[0]["size_sqm"])
        self.assertEqual("2026-08-01", snapshot.listings[0]["availableFrom"])
        required_payload_keys = {"PageIndex", "PageSize", "MaxRent", "ZipCodes", "TypeIds", "TagIds", "MinRooms", "MaxRooms", "MinSize", "MaxSize", "MinFloor", "MaxFloor", "AcquisitionDateFrom", "AcquisitionDateTo", "OnlyAvailable", "RentalPeriod", "FacilityIds", "AddressQuery"}
        self.assertEqual(required_payload_keys, set(pages_payloads[0]))

    def test_missing_cases_key_is_not_treated_as_empty(self):
        with self.assertRaisesRegex(Exception, "Cases key"):
            fetch_lejeboligmaegleren(
                lambda _url, _payload: {"HasMorePages": False},
                lambda _url: [],
            )
```

- [ ] **Step 2: Run the new test and verify the missing function**

Run:

```powershell
python -m unittest test_landlord_sources.LejeboligmaeglerenSourceTests -v
```

Expected: import failure for `fetch_lejeboligmaegleren`.

- [ ] **Step 3: Implement the JSON adapter with an empty-page stop**

Add to `housing_sources/landlords.py`:

```python
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


def _dictionary(records):
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
    unit_type_names = _dictionary(fetch_json(LEJE_UNIT_TYPES_URL))
    tag_names = _dictionary(fetch_json(LEJE_TAGS_URL))
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
                raise SourceContractError(f"Lejeboligmægleren case {case_id} City is not an object")
            postcode = str(city_record.get("ZipCode") or city_record.get("zipCode") or "").strip()
            city = str(city_record.get("Name") or city_record.get("name") or "").strip()
            status = _lej_state(case.get("State") or case.get("state"))
            if status is None:
                continue
            restriction_text = _lej_restriction_text(case, unit_type_names, tag_names)
            listing = {
                "id": f"lejeboligmaegleren:{case_id}",
                "status": status,
                "name": address,
                "price": {"amount": case.get("Rent") or case.get("rent")},
                "location": {"formatted": f"{address}, {postcode} {city}".strip(", ")},
                "availableFrom": case.get("AcquisitionDate") or "See link for info",
                "url": f"https://lejeboligmaegleren.dk/cases/{case_id}/",
                "source": "Lejeboligmægleren",
                "transaction_type": "rent",
                "price_period": "month",
                "rooms": case.get("Rooms") or case.get("rooms"),
                "size_sqm": case.get("Size") or case.get("size"),
                "raw_text": restriction_text,
                "canonical_key": canonical_listing_key(f"{address}, {postcode} {city}", "rent"),
                "source_priority": 20,
            }
            if not contains_restricted_eligibility(restriction_text) and listing_matches_policy(listing):
                listings.append(listing)
        if len(cases) < page_size:
            break
    return SourceSnapshot(source="Lejeboligmægleren", listings=listings)
```

- [ ] **Step 4: Run focused and complete tests**

Run:

```powershell
python -m unittest test_landlord_sources.LejeboligmaeglerenSourceTests -v
python -m unittest discover -v
```

Expected: all tests pass even though the fixture's `HasMorePages` remains true on the empty page.

- [ ] **Step 5: Commit the Lejeboligmægleren adapter**

```powershell
git add housing_sources/landlords.py test_landlord_sources.py
git commit -m "Add Lejeboligmaegleren rental source"
```

### Task 8: Norhjem Canonical-Overview Adapter

**Files:**
- Modify: `housing_sources/landlords.py`
- Modify: `test_landlord_sources.py`

- [ ] **Step 1: Add failing canonical-overview and student-exclusion tests**

Append to `test_landlord_sources.py`:

```python
from housing_sources.landlords import fetch_norhjem, parse_norhjem_results


NORHJEM_RESULTS = [
    {"address": "Willemoesgade 1, 2. tv.", "zipCode": 2100, "city": "København Ø", "url": "/ejendomme/osterbro/willemoesgade-1-2-tv/", "price": 9150, "rooms": 2, "area": 55, "status": "Ledig", "moveInDate": "2026-08-01", "type": "Lejlighed"},
    {"address": "Amagerbrogade 10", "zipCode": 2300, "city": "København S", "url": "/ejendomme/amager/student-1/", "price": 5523, "rooms": 1, "area": 30, "status": "Ledig", "moveInDate": "2026-09-01", "type": "Lejlighed"},
    {"address": "Roskildevej 33", "zipCode": 2000, "city": "Frederiksberg", "url": "/ejendomme/frederiksberg/reserved/", "price": 12500, "rooms": 2, "area": 65, "status": "Reserveret", "moveInDate": "2026-10-01", "type": "Lejlighed"},
]


class NorhjemSourceTests(unittest.TestCase):
    def test_normalizes_live_api_results_and_retains_reserved_state(self):
        listings = parse_norhjem_results(NORHJEM_RESULTS, blocked_urls={"/ejendomme/amager/student-1/"})
        self.assertEqual(
            ["norhjem:/ejendomme/osterbro/willemoesgade-1-2-tv/", "norhjem:/ejendomme/frederiksberg/reserved/"],
            [item["id"] for item in listings],
        )
        self.assertEqual(["Available", "Reserved"], [item["status"] for item in listings])

    def test_fetch_uses_canonical_form_api_and_detail_restriction_guard(self):
        form_calls = []
        detail_calls = []

        def post_form(_url, payload):
            form_calls.append(payload)
            if payload.get("facilities") == "Kun for studerende":
                return [NORHJEM_RESULTS[1]]
            return NORHJEM_RESULTS

        def fetch_text(url):
            detail_calls.append(url)
            return "<main>Almindelig lejebolig uden medlemskrav</main>"

        snapshot = fetch_norhjem(post_form, fetch_text)
        self.assertEqual(
            [{"maxPrice": "18000", "sort": ""}, {"maxPrice": "18000", "sort": "", "facilities": "Kun for studerende"}],
            form_calls,
        )
        self.assertEqual(2, len(snapshot.listings))
        self.assertTrue(all(url.startswith("https://norhjem.dk/ejendomme/") for url in detail_calls))

    def test_wrong_api_shape_is_not_a_valid_empty_feed(self):
        with self.assertRaisesRegex(Exception, "JSON list"):
            fetch_norhjem(lambda _url, _payload: {"results": []}, lambda _url: "")
```

- [ ] **Step 2: Run the new tests and verify the missing function**

Run:

```powershell
python -m unittest test_landlord_sources.NorhjemSourceTests -v
```

Expected: import failure for `fetch_norhjem` or `parse_norhjem_results`.

- [ ] **Step 3: Implement canonical API membership plus detail restriction screening**

Add to `housing_sources/landlords.py`:

```python
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
            "raw_text": " ".join(str(record.get(field) or "") for field in ("type", "leasePeriod", "description")),
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
    blocked_urls = {str(record.get("url")) for record in student_records if isinstance(record, dict)}
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
        main = re.search(r"<main\b[^>]*>([\s\S]*?)</main>", detail_html, re.IGNORECASE)
        detail_text = _plain_text(main.group(1) if main else detail_html)
        listing["raw_text"] = f"{listing.get('raw_text', '')} {detail_text}"
        if not contains_restricted_eligibility(listing["raw_text"]):
            listings.append(listing)
    if candidates and valid_details == 0:
        raise SourceContractError("No Norhjem detail matched the restriction-screening contract")
    return SourceSnapshot(source="Norhjem", listings=listings)
```

- [ ] **Step 4: Run focused and complete tests**

Run:

```powershell
python -m unittest test_landlord_sources.NorhjemSourceTests -v
python -m unittest discover -v
```

Expected: all tests pass; the student URL is absent, the reserved home is retained for a later available transition, and a wrong API shape is not accepted as an empty inventory.

- [ ] **Step 5: Commit the Norhjem adapter**

```powershell
git add housing_sources/landlords.py test_landlord_sources.py
git commit -m "Add canonical Norhjem vacancy source"
```

### Task 9: AKF Classification Inside the Existing Propstep Feed

**Files:**
- Modify: `watcher.py:972-1051`
- Modify: `test_watcher.py:70-107`

- [ ] **Step 1: Add failing AKF identity, availability, and restriction tests**

Add to `PropstepTests` in `test_watcher.py`:

```python
    @patch("watcher.post_json")
    def test_classifies_public_akf_cards_without_changing_propstep_id(self, mock_post_json):
        mock_post_json.return_value = {
            "searchResults": [
                {
                    "companyId": "5db6d00f4e5146201ae72ada",
                    "properties": [
                        {
                            "id": "akf-live",
                            "slug": "akf-live",
                            "name": "Nørrebrogade 10",
                            "transactionStatus": 1,
                            "location": {"address": "Nørrebrogade 10", "postalcode": "2200", "city": "København N"},
                            "transactionDetails": {"price": 1750000},
                            "propertyDetails": {"size": 70, "rooms": 2, "onlyFor": ""},
                        },
                        {
                            "id": "akf-reserved",
                            "slug": "akf-reserved",
                            "name": "Nørrebrogade 12",
                            "transactionStatus": 2,
                            "location": {"address": "Nørrebrogade 12", "postalcode": "2200", "city": "København N"},
                            "transactionDetails": {"price": 1700000},
                            "propertyDetails": {"onlyFor": ""},
                        },
                        {
                            "id": "akf-student",
                            "slug": "akf-student",
                            "name": "Nørrebrogade 14",
                            "transactionStatus": 1,
                            "location": {"address": "Nørrebrogade 14", "postalcode": "2200", "city": "København N"},
                            "transactionDetails": {"price": 900000},
                            "propertyDetails": {"onlyFor": "Kun for studerende"},
                        },
                        {
                            "id": "akf-waitlist",
                            "slug": "akf-waitlist",
                            "name": "Nørrebrogade 16",
                            "transactionStatus": 1,
                            "waitingList": True,
                            "location": {"address": "Nørrebrogade 16", "postalcode": "2200", "city": "København N"},
                            "transactionDetails": {"price": 1000000},
                            "propertyDetails": {"onlyFor": ""},
                        },
                        {
                            "id": "akf-localized-restriction",
                            "slug": "akf-localized-restriction",
                            "name": "Nørrebrogade 18",
                            "transactionStatus": 1,
                            "location": {"address": "Nørrebrogade 18", "postalcode": "2200", "city": "København N"},
                            "transactionDetails": {"price": 1100000},
                            "propertyDetails": {"onlyFor": "", "langToDescription": {"da": "Kun for seniorer"}},
                        },
                    ],
                }
            ],
            "totalProperties": 5,
        }
        listings = watcher.fetch_propstep_apartments()
        self.assertEqual(["propstep:akf-live"], [item["id"] for item in listings])
        self.assertEqual("AKF via Propstep", listings[0]["source"])
        mock_post_json.assert_called_once()
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```powershell
python -m unittest test_watcher.PropstepTests.test_classifies_public_akf_cards_without_changing_propstep_id -v
```

Expected: FAIL because all three properties are currently normalized as ordinary Propstep records.

- [ ] **Step 3: Add AKF-specific guards without another fetch**

Add near `PROPSTEP_SEARCH_URL` in `watcher.py`:

```python
AKF_PROPSTEP_COMPANY_ID = "5db6d00f4e5146201ae72ada"
```

Inside the existing group/property loop, before appending a listing, add:

```python
                company_id = prop.get("companyId") or group.get("companyId")
                is_akf = company_id == AKF_PROPSTEP_COMPANY_ID
                property_details = prop.get("propertyDetails") or {}
                restricted_text = ""
                if is_akf:
                    if prop.get("transactionStatus") != 1:
                        continue
                    is_waitlist = any(
                        is_truthy(value)
                        for value in (
                            prop.get("waitingList"),
                            prop.get("isWaitingList"),
                            property_details.get("waitingList"),
                            group.get("waitingList"),
                        )
                    )
                    if is_waitlist:
                        continue
                    restricted_text = " ".join(
                        str(value or "")
                        for value in (
                            property_details.get("onlyFor"),
                            property_details.get("langToDescription"),
                            prop.get("description"),
                            prop.get("langToDescription"),
                            group.get("description"),
                            group.get("langToDescription"),
                        )
                    )
                    if contains_restricted_eligibility(restricted_text):
                        continue
                source_name = "AKF via Propstep" if is_akf else "Propstep"
```

Reuse the already computed `property_details`, set the returned listing's `source` to `source_name`, copy `restricted_text` into its `raw_text` field, set `canonical_key=canonical_listing_key(location_text, "rent")`, and set `source_priority=20`. This gives cross-cycle deduplication the same complete unit address while preserving `propstep:{property.id}`. Do not add an AKF URL, API request, or `akf:` ID. The local waitlist-field check is required even though the request payload also sends `waitingLists: false`.

- [ ] **Step 4: Run Propstep and complete tests**

Run:

```powershell
python -m unittest test_watcher.PropstepTests -v
python -m unittest discover -v
```

Expected: all tests pass and the original non-AKF transaction-status test remains unchanged.

- [ ] **Step 5: Commit the AKF classification**

```powershell
git add watcher.py test_watcher.py
git commit -m "Classify public AKF listings in Propstep"
```

### Task 10: RLE Structured Vacancy and Readiness Adapter

**Files:**
- Create: `housing_sources/readiness.py`
- Create: `test_readiness_sources.py`

- [ ] **Step 1: Write failing no-vacancy, residential, and ambiguous-change tests**

Create `test_readiness_sources.py`:

```python
import unittest

from housing_sources.readiness import fetch_rle, parse_rle_document


class RLESourceTests(unittest.TestCase):
    def test_no_vacancy_document_returns_status_event_and_no_listing(self):
        document = {
            "_updatedAt": "2026-01-21T14:20:35Z",
            "content": [
                {
                    "_key": "empty",
                    "_type": "textAndImageBlock",
                    "text": [{"children": [{"text": "Vi har på nuværende tidspunkt ingen ledige ejendomme."}]}],
                }
            ],
        }
        snapshot = parse_rle_document(document)
        self.assertEqual([], snapshot.listings)
        self.assertEqual("No residential vacancies", snapshot.events[0]["headline"])

    def test_parses_residential_block_and_rejects_commercial_block(self):
        document = {
            "content": [
                {"_key": "home", "_type": "vacancy", "use": "bolig", "status": "ledig", "address": "Nørrebrogade 10", "postalCode": 2200, "city": "København N", "monthlyRent": 17500, "description": "Privat lejlighed"},
                {"_key": "shop", "_type": "vacancy", "use": "erhverv", "status": "ledig", "address": "Østerbrogade 1", "postalCode": 2100, "city": "København Ø", "monthlyRent": 10000},
                {"_key": "student", "_type": "vacancy", "use": "bolig", "status": "ledig", "address": "Nørrebrogade 12", "postalCode": 2200, "city": "København N", "monthlyRent": 7000, "eligibility": "Kun for studerende"},
            ]
        }
        snapshot = parse_rle_document(document)
        self.assertEqual(["rle:home"], [item["id"] for item in snapshot.listings])
        self.assertEqual([], snapshot.events)
        self.assertEqual("rent:norrebrogade 10 2200 kobenhavn n", snapshot.listings[0]["canonical_key"])

    def test_descriptive_portable_text_does_not_become_a_fake_vacancy(self):
        document = {"content": [{"_key": "copy", "text": [{"children": [{"text": "Vi ejer boliger på Nørrebrogade 10, 2200 København N til en værdi af 17.500 kr."}]}]}]}
        snapshot = parse_rle_document(document)
        self.assertEqual([], snapshot.listings)
        self.assertEqual("RLE changed - inspect now", snapshot.events[0]["headline"])

    def test_unclassified_replacement_creates_inspection_event(self):
        snapshot = parse_rle_document({"content": [{"_key": "changed", "text": "Nyt indhold offentliggjort"}]})
        self.assertEqual([], snapshot.listings)
        self.assertEqual("RLE changed - inspect now", snapshot.events[0]["headline"])

    def test_fetch_uses_the_public_sanity_document(self):
        calls = []
        snapshot = fetch_rle(lambda url: calls.append(url) or {"result": {"content": []}})
        self.assertEqual(1, len(calls))
        self.assertIn("api.sanity.io", calls[0])
        self.assertEqual("RLE", snapshot.source)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests and verify the module is absent**

Run:

```powershell
python -m unittest test_readiness_sources.RLESourceTests -v
```

Expected: `ModuleNotFoundError: No module named 'housing_sources.readiness'`.

- [ ] **Step 3: Implement Sanity flattening and fail-safe classification**

Create `housing_sources/readiness.py` with the RLE portion:

```python
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
RLE_URL = "https://k56dk3dw.api.sanity.io/v2025-07-23/data/query/production?" + urlencode({"query": RLE_QUERY})


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


def _readiness_event(event_id, source, headline, description, signature, url, urgent=False, registration_closed=False):
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
        if use not in {"bolig", "privat bolig", "lejlighed"} or status_text not in {"ledig", "available"}:
            continue
        restriction_text = " ".join(str(block.get(field) or "") for field in ("eligibility", "requirements", "description"))
        if contains_commercial_use(f"type {block.get('use', '')}") or contains_restricted_eligibility(restriction_text):
            continue
        postcode = extract_postcode(block.get("postalCode"))
        price = extract_amount(block.get("monthlyRent"))
        address = f"{block.get('address', '')}, {block.get('postalCode', '')} {block.get('city', '')}".strip(", ")
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
        }
        if postcode and listing_matches_policy(listing):
            listings.append(listing)
    if no_vacancies and listings:
        raise SourceContractError("RLE document contradicts itself with vacancies and a no-vacancy statement")
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
```

- [ ] **Step 4: Run focused and complete tests**

Run:

```powershell
python -m unittest test_readiness_sources.RLESourceTests -v
python -m unittest discover -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the RLE adapter**

```powershell
git add housing_sources/readiness.py test_readiness_sources.py
git commit -m "Add structured RLE vacancy monitor"
```

### Task 11: CPH Homes Material-Change Readiness Monitor

**Files:**
- Modify: `housing_sources/readiness.py`
- Modify: `test_readiness_sources.py`

- [ ] **Step 1: Add failing stable-baseline and availability-signal tests**

Append to `test_readiness_sources.py`:

```python
from housing_sources.readiness import CPH_DOCUMENT_URLS, discover_cphomes_post_urls, fetch_cphomes, parse_cphomes_documents


def cph_page(body, head=""):
    return f"<html><head>{head}</head><body><main><h1>CPH Homes</h1>{body}</main><footer>Footer</footer></body></html>"


class CPHHomesSourceTests(unittest.TestCase):
    def test_static_portfolio_returns_nonurgent_readiness_state(self):
        documents = {"https://cphhomes.dk/holmen/": cph_page("<p>Attraktive boliger på Holmen</p>")}
        snapshot = parse_cphomes_documents(documents)
        self.assertEqual([], snapshot.listings)
        self.assertFalse(snapshot.events[0]["urgent"])
        self.assertEqual("CPH Homes monitoring ready", snapshot.events[0]["headline"])

    def test_availability_language_creates_inspection_signal_not_fake_listing(self):
        documents = {"https://cphhomes.dk/sydhavnen/": cph_page("<p>Ledig lejlighed, 2450 København SV, husleje 17.500 kr. Kontakt os</p>")}
        snapshot = parse_cphomes_documents(documents)
        self.assertEqual([], snapshot.listings)
        self.assertFalse(snapshot.events[0]["urgent"])
        self.assertEqual("CPH Homes availability signal - inspect now", snapshot.events[0]["headline"])
        self.assertEqual("https://cphhomes.dk/sydhavnen/", snapshot.events[0]["url"])

    def test_modified_timestamp_alone_does_not_change_signature(self):
        url = "https://cphhomes.dk/holmen/"
        first = {url: cph_page("<p>Samme indhold</p>", '<meta property="article:modified_time" content="2019-01-01">')}
        second = {url: cph_page("<p>Samme indhold</p>", '<meta property="article:modified_time" content="2026-07-13">')}
        self.assertEqual(
            parse_cphomes_documents(first).events[0]["signature"],
            parse_cphomes_documents(second).events[0]["signature"],
        )

    def test_new_same_host_application_link_is_recorded_as_evidence(self):
        url = "https://cphhomes.dk/engholmene/"
        event = parse_cphomes_documents({url: cph_page('<a href="/kontakt/">Skriv dig op</a>')}).events[0]
        self.assertIn("application-link:https://cphhomes.dk/kontakt/", event["signals"])

    def test_unknown_page_is_ignored_and_new_external_action_is_review_evidence(self):
        unknown = {"https://cphhomes.dk/valby/": cph_page("<p>Ledig bolig, 2500 Valby</p>")}
        self.assertEqual([], parse_cphomes_documents(unknown).events)
        url = "https://cphhomes.dk/engholmene/"
        event = parse_cphomes_documents({url: cph_page('<a href="https://apply.example/bolig">Skriv dig op</a>')}).events[0]
        self.assertEqual(url, event["url"])
        self.assertIn("external-application-review:apply.example", event["signals"])

    def test_discovers_only_same_host_https_article_links(self):
        home = cph_page(
            '<article><a href="/nyheder/ledig-paa-holmen/">Ny bolig</a></article>'
            '<article><a href="https://evil.example/post">Falsk</a></article>'
        )
        self.assertEqual(
            ["https://cphhomes.dk/nyheder/ledig-paa-holmen/"],
            discover_cphomes_post_urls(home),
        )

    def test_fetch_uses_only_pinned_https_canonical_pages(self):
        calls = []

        def fetch_text(url):
            calls.append(url)
            return cph_page("<p>Relevant portfolio</p>")

        snapshot = fetch_cphomes(fetch_text)
        self.assertEqual(set(CPH_DOCUMENT_URLS.values()), set(calls))
        self.assertEqual(len(CPH_DOCUMENT_URLS), len(snapshot.events))
        self.assertTrue(all(event["url"].startswith("https://cphhomes.dk/") for event in snapshot.events))

    def test_fetch_follows_newly_published_same_host_post_once(self):
        post_url = "https://cphhomes.dk/nyheder/ledig-paa-holmen/"
        calls = []

        def fetch_text(url):
            calls.append(url)
            if url == CPH_DOCUMENT_URLS["home"]:
                return cph_page(f'<article><a href="{post_url}">Ny bolig</a></article>')
            if url == post_url:
                return cph_page("<p>Ledig lejlighed på Holmen</p>")
            return cph_page("<p>Relevant portfolio</p>")

        snapshot = fetch_cphomes(fetch_text)
        self.assertEqual(1, calls.count(post_url))
        self.assertTrue(any(event["id"].startswith("readiness:cphhomes:post:") for event in snapshot.events))
```

- [ ] **Step 2: Run the new test class and verify missing interfaces**

Run:

```powershell
python -m unittest test_readiness_sources.CPHHomesSourceTests -v
```

Expected: import failure for `fetch_cphomes`, `parse_cphomes_documents`, or `CPH_DOCUMENT_URLS`.

- [ ] **Step 3: Implement pinned read-only revision monitoring**

Add to `housing_sources/readiness.py`:

```python
CPH_DOCUMENT_URLS = {
    "home": "https://cphhomes.dk/",
    "holmen": "https://cphhomes.dk/holmen/",
    "sydhavnen": "https://cphhomes.dk/sydhavnen/",
    "orestaden": "https://cphhomes.dk/orestaden/",
    "bryggen": "https://cphhomes.dk/bryggen/",
    "engholmene": "https://cphhomes.dk/engholmene/",
}
CPH_AVAILABILITY_TERMS = ("ledig", "udlejes", "husleje", "book fremvisning", "ansog", "skriv dig op", "tilmelding", "interesseliste", "boliger til leje")
CPH_EXTERNAL_ACTION_HOSTS = frozenset()  # Add only exact hosts backed by a captured live application link.
CPH_MAX_DISCOVERED_POSTS = 50


def _cph_main(html):
    main = re.search(r"<main\b[^>]*>([\s\S]*?)</main>", html, re.IGNORECASE)
    if not main:
        raise SourceContractError("CPH Homes page has no main content container")
    return re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", main.group(1), flags=re.IGNORECASE)


def _safe_cph_link(base_url, href):
    absolute = urljoin(base_url, unescape(href))
    parsed = urlparse(absolute)
    if parsed.scheme != "https" or parsed.hostname not in {"cphhomes.dk", "www.cphhomes.dk"} or parsed.port not in {None, 443}:
        return None
    return parsed._replace(query="", fragment="").geturl()


def discover_cphomes_post_urls(home_html):
    content = _cph_main(home_html)
    discovered = set()
    for article in re.findall(r"<article\b[^>]*>([\s\S]*?)</article>", content, re.IGNORECASE):
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
    for href, label in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', content, re.IGNORECASE):
        if any(term in normalize_text(label) for term in CPH_AVAILABILITY_TERMS):
            absolute = urljoin(url, unescape(href))
            parsed = urlparse(absolute)
            if parsed.scheme != "https" or not parsed.hostname or parsed.port not in {None, 443}:
                continue
            if parsed.hostname in {"cphhomes.dk", "www.cphhomes.dk"} or parsed.hostname in CPH_EXTERNAL_ACTION_HOSTS:
                signals.add(f"application-link:{absolute}")
            else:
                signals.add(f"external-application-review:{parsed.hostname}")
    has_evidence = bool(matched_terms)
    headline = "CPH Homes availability signal - inspect now" if has_evidence else "CPH Homes monitoring ready"
    event = _readiness_event(
        f"readiness:cphhomes:{key}",
        "CPH Homes",
        headline,
        "This relevant page contains availability evidence; inspect it before applying."
        if has_evidence
        else "This relevant CPH Homes page is being monitored for material changes.",
        _signature(f"{normalized}|{'|'.join(sorted(signals))}"),
        url,
        urgent=False,
    )
    event.update(
        {
            "signals": sorted(signals),
            "baseline_headline": headline,
            "change_headline": "CPH Homes availability signal - inspect now" if has_evidence else "CPH Homes changed - inspect now",
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
            raise SourceContractError("CPH Homes discovered post URL is not an exact same-host HTTPS URL")
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
```

Import `urljoin` and `urlparse` from `urllib.parse`. Do not use CPH Homes' plain-HTTP WordPress endpoint and do not create an unverified listing from these pages. Every initial read is a pinned HTTPS canonical page. Newly published posts are followed only when an HTTPS same-host link appears inside a scoped `<article>` on the pinned homepage, with a defensive count limit; arbitrary navigation, out-of-area slugs, scripts, and external hosts are never fetched. Every emitted event URL is either a pinned page or one of those same-host discovered posts, never page-controlled WordPress metadata.

An outbound HTTPS application action is still evidence: an exact host in `CPH_EXTERNAL_ACTION_HOSTS` contributes the reviewed full URL, while a previously unseen host contributes only `external-application-review:<host>` and keeps the Discord embed pointed at the safe CPH Homes page. Extend the allowlist only after saving a focused fixture from the live source. Each document gets its own stable event so a revision identifies the page to inspect.

Signatures use only scoped `<main>` content and safe signals, so head metadata, scripts, navigation, and footer churn are silent. Ongoing readiness processing compares each document's stored `signals` set and treats newly introduced price, preferred-postcode, availability-term, or safe application-link evidence as inspection evidence, not as a confirmed vacancy or Værnedamsvej application opening.

- [ ] **Step 4: Run focused and complete tests**

Run:

```powershell
python -m unittest test_readiness_sources.CPHHomesSourceTests -v
python -m unittest discover -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the CPH Homes monitor**

```powershell
git add housing_sources/readiness.py test_readiness_sources.py
git commit -m "Add CPH Homes readiness monitor"
```

### Task 12: Værnedamsvej Project and Application-Opening Monitor

**Files:**
- Modify: `housing_sources/readiness.py`
- Modify: `test_readiness_sources.py`

- [ ] **Step 1: Add failing latest-update and negation-aware opening tests**

Append to `test_readiness_sources.py`:

```python
from housing_sources.readiness import (
    DFE_PROJECT_URL,
    detect_application_signal,
    extract_application_actions,
    fetch_vaernedamsvej,
    parse_latest_project_update,
)


PROJECT_HTML = """
<h2>Projektet tager næste skridt</h2>
<p>Opdateret den 20. maj 2026</p>
<p>Vi afventer fortsat de nødvendige byggetilladelser for boligbyggeriet.</p>
<h2>Status på omlægning af fjernvarme</h2><p>Opdateret den 14. april 2026</p>
"""
DFE_CLOSED_HTML = "<main>Det er endnu ikke muligt at skrive sig op til en bolig eller et nyhedsbrev.</main>"
DFE_OPEN_HTML = '<main><a href="/skriv-dig-op/">Skriv dig op til en bolig</a></main>'


class VaernedamsvejSourceTests(unittest.TestCase):
    def test_extracts_only_the_newest_project_update(self):
        title, date, paragraph = parse_latest_project_update(PROJECT_HTML)
        self.assertEqual("Projektet tager næste skridt", title)
        self.assertEqual("20. maj 2026", date)
        self.assertIn("byggetilladelser", paragraph)

    def test_newest_update_is_selected_even_when_sections_are_not_newest_first(self):
        reversed_html = """
        <h2>Ældre</h2><p>Opdateret den 14. april 2026</p><p>Gammel status.</p>
        <h2>Nyeste</h2><p>Opdateret den 20. maj 2026</p><p>Ny status.</p>
        """
        self.assertEqual("Nyeste", parse_latest_project_update(reversed_html)[0])

    def test_negated_registration_text_is_not_an_opening(self):
        self.assertFalse(detect_application_signal(DFE_CLOSED_HTML))
        self.assertTrue(detect_application_signal(DFE_OPEN_HTML))

    def test_footer_newsletter_and_unrelated_first_link_do_not_mask_real_action(self):
        footer_only = '<main><p>Projektstatus</p></main><footer><a href="/newsletter">Tilmelding til nyhedsbrev</a></footer>'
        multiple = '<main><a href="/kontakt">Kontakt</a><a href="/ansog/">Skriv dig op til en bolig</a></main>'
        self.assertFalse(detect_application_signal(footer_only))
        actions = extract_application_actions(multiple, DFE_PROJECT_URL)
        self.assertEqual(["https://www.dfe.dk/ansog/"], [action["url"] for action in actions])

    def test_application_form_action_is_detected(self):
        html = '<main><form action="/bolig-tilmelding/"><button>Tilmelding til bolig</button></form></main>'
        actions = extract_application_actions(html, DFE_PROJECT_URL)
        self.assertEqual("https://www.dfe.dk/bolig-tilmelding/", actions[0]["url"])

    def test_closed_statement_wins_over_generic_inventory_link(self):
        contradictory = '<main><p>Det er endnu ikke muligt at skrive sig op til en bolig.</p><a href="/boliger/">Se boliger</a></main>'

        def fetch_text(url):
            return PROJECT_HTML if "status-pa-projektet" in url else contradictory

        event = fetch_vaernedamsvej(fetch_text).events[0]
        self.assertTrue(event["registration_closed"])
        self.assertFalse(event["urgent"])
        self.assertEqual("project_update", event["kind"])

    def test_alternate_closed_wording_and_weak_action_are_not_an_opening(self):
        closed = '<main><p>Ansøgning til boligerne er endnu ikke åben.</p><a href="/boliger/">Se boliger</a></main>'

        def fetch_text(url):
            return PROJECT_HTML if "status-pa-projektet" in url else closed

        event = fetch_vaernedamsvej(fetch_text).events[0]
        self.assertTrue(event["registration_closed"])
        self.assertFalse(event["urgent"])

    def test_fetch_combines_project_and_dfe_state(self):
        def fetch_text(url):
            return PROJECT_HTML if "status-pa-projektet" in url else DFE_CLOSED_HTML

        snapshot = fetch_vaernedamsvej(fetch_text)
        event = snapshot.events[0]
        self.assertEqual("Projektet tager næste skridt — 20. maj 2026", event["headline"])
        self.assertTrue(event["registration_closed"])
        self.assertFalse(event["urgent"])

    def test_positive_application_link_is_urgent(self):
        def fetch_text(url):
            return PROJECT_HTML if "status-pa-projektet" in url else DFE_OPEN_HTML

        event = fetch_vaernedamsvej(fetch_text).events[0]
        self.assertTrue(event["urgent"])
        self.assertFalse(event["registration_closed"])
        self.assertEqual("application_opening", event["kind"])
        self.assertEqual("https://www.dfe.dk/skriv-dig-op/", event["url"])

    def test_open_inventory_or_viewing_action_is_strong_but_generic_see_homes_is_not(self):
        strong = '<main><a href="/fremvisning/">Book fremvisning</a></main>'
        weak = '<main><a href="/boliger/">Se boliger</a></main>'
        self.assertTrue(detect_application_signal(strong))
        self.assertFalse(detect_application_signal(weak))

    def test_direct_dfe_application_outranks_generic_project_action(self):
        project_with_action = PROJECT_HTML + '<main><a href="/se-boliger/">Se boliger</a></main>'

        def fetch_text(url):
            return project_with_action if "status-pa-projektet" in url else DFE_OPEN_HTML

        event = fetch_vaernedamsvej(fetch_text).events[0]
        self.assertEqual("https://www.dfe.dk/skriv-dig-op/", event["url"])
```

- [ ] **Step 2: Run the new test class and verify missing interfaces**

Run:

```powershell
python -m unittest test_readiness_sources.VaernedamsvejSourceTests -v
```

Expected: import failures for the Værnedamsvej functions.

- [ ] **Step 3: Implement latest-update extraction and negation-aware matching**

Add to `housing_sources/readiness.py`:

```python
PROJECT_STATUS_URL = "https://denfranskeskolevaernedamsvej.dk/status-pa-projektet/"
DFE_PROJECT_URL = "https://www.dfe.dk/bolig/bolig/the-french-school-at-vaernedamsvej"
APPLICATION_TERMS = (
    "skriv dig op",
    "opskrivning",
    "interesseliste",
    "tilmelding",
    "ansog",
    "ledige boliger",
    "boliger til leje",
    "book fremvisning",
    "se boliger",
)
NEGATED_APPLICATION_PATTERNS = (
    r"ikke.{0,80}(?:skrive sig op|opskrivning|tilmelding|ansog)",
    r"(?:ansogning|tilmelding|opskrivning).{0,50}(?:ikke aben|lukket|senere)",
    r"(?:kan|er).{0,40}ikke.{0,80}(?:ansog|tilmeld|opskriv|skrive sig op)",
    r"ingen.{0,50}(?:ansogning|tilmelding|opskrivning)",
    r"ikke.{0,80}muligt.{0,80}(?:bolig|nyhedsbrev)",
)
DANISH_MONTHS = {
    "januar": 1,
    "februar": 2,
    "marts": 3,
    "april": 4,
    "maj": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}
APPLICATION_HOSTS = {"denfranskeskolevaernedamsvej.dk", "www.dfe.dk", "dfe.dk"}


def _plain_html(value):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _danish_date_key(value):
    parts = normalize_text(value).split()
    if len(parts) < 3 or parts[1] not in DANISH_MONTHS:
        raise SourceContractError(f"Unrecognized Værnedamsvej update date: {value}")
    return int(parts[2]), DANISH_MONTHS[parts[1]], int(parts[0])


def parse_latest_project_update(html):
    headings = list(re.finditer(r"<h[1-3][^>]*>([\s\S]*?)</h[1-3]>", html, re.IGNORECASE))
    updates = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(html)
        section = html[heading.end() : end]
        date_match = re.search(r"Opdateret den\s+([^<]+)", section, re.IGNORECASE)
        paragraphs = re.findall(r"<p[^>]*>([\s\S]*?)</p>", section, re.IGNORECASE)
        if date_match and paragraphs:
            title = _plain_html(heading.group(1))
            date = _plain_html(date_match.group(1))
            body_paragraphs = [_plain_html(value) for value in paragraphs]
            paragraph = next((value for value in body_paragraphs if not normalize_text(value).startswith("opdateret den")), body_paragraphs[0])
            updates.append((_danish_date_key(date), title, date, paragraph))
    if not updates:
        raise SourceContractError("Værnedamsvej latest project update is missing")
    _key, title, date, paragraph = max(updates, key=lambda value: value[0])
    return title, date, paragraph


def _action_confidence(value):
    scrubbed = normalize_text(value)
    for pattern in NEGATED_APPLICATION_PATTERNS:
        scrubbed = re.sub(pattern, " ", scrubbed, flags=re.IGNORECASE)
    strong_terms = (
        "skriv dig op",
        "opskrivning",
        "interesseliste",
        "tilmelding",
        "ansog",
        "ledige boliger",
        "boliger til leje",
        "book fremvisning",
    )
    if any(term in scrubbed for term in strong_terms):
        return 2
    return 1 if any(term in scrubbed for term in APPLICATION_TERMS) else 0


def _safe_project_action(base_url, value):
    target = urljoin(base_url, value or base_url)
    parsed = urlparse(target)
    return target if parsed.scheme == "https" and parsed.hostname in APPLICATION_HOSTS else None


def extract_application_actions(html, base_url, origin="project"):
    main = re.search(r"<main\b[^>]*>([\s\S]*?)</main>", html, re.IGNORECASE)
    scope = main.group(1) if main else ""
    actions = []
    form_pattern = r"<form\b([^>]*)>([\s\S]*?)</form>"
    for attributes, body in re.findall(form_pattern, scope, re.IGNORECASE):
        confidence = _action_confidence(_plain_html(body))
        if not confidence:
            continue
        action_match = re.search(r'action=["\']([^"\']+)["\']', attributes, re.IGNORECASE)
        target = _safe_project_action(base_url, action_match.group(1) if action_match else base_url)
        if not target:
            continue
        actions.append({"url": target, "label": _plain_html(body), "confidence": 3, "origin": origin})
    scope_without_forms = re.sub(form_pattern, " ", scope, flags=re.IGNORECASE)
    for tag, attributes, body in re.findall(r"<(a|button)\b([^>]*)>([\s\S]*?)</\1>", scope_without_forms, re.IGNORECASE):
        label = _plain_html(body)
        confidence = _action_confidence(label)
        if not confidence:
            continue
        target_match = re.search(r'(?:href|formaction)=["\']([^"\']+)["\']', attributes, re.IGNORECASE)
        target = _safe_project_action(base_url, target_match.group(1) if target_match else base_url)
        if not target:
            continue
        actions.append({"url": target, "label": label, "confidence": confidence, "origin": origin})
    deduplicated = {}
    for action in actions:
        deduplicated[(action["url"], normalize_text(action["label"]))] = action
    return sorted(deduplicated.values(), key=lambda action: (-action["confidence"], action["url"], normalize_text(action["label"])))


def detect_application_signal(html):
    main = re.search(r"<main\b[^>]*>([\s\S]*?)</main>", html, re.IGNORECASE)
    normalized_main = normalize_text(_plain_html(main.group(1) if main else ""))
    if any(re.search(pattern, normalized_main, re.IGNORECASE) for pattern in NEGATED_APPLICATION_PATTERNS):
        return False
    return any(action["confidence"] >= 2 for action in extract_application_actions(html, DFE_PROJECT_URL, origin="dfe"))


def fetch_vaernedamsvej(fetch_text):
    project_html = fetch_text(PROJECT_STATUS_URL)
    dfe_html = fetch_text(DFE_PROJECT_URL)
    title, date, paragraph = parse_latest_project_update(project_html)
    dfe_main = re.search(r"<main\b[^>]*>([\s\S]*?)</main>", dfe_html, re.IGNORECASE)
    normalized_dfe = normalize_text(_plain_html(dfe_main.group(1) if dfe_main else ""))
    registration_closed = any(re.search(pattern, normalized_dfe, re.IGNORECASE) for pattern in NEGATED_APPLICATION_PATTERNS)
    actions = extract_application_actions(project_html, PROJECT_STATUS_URL, origin="project") + extract_application_actions(dfe_html, DFE_PROJECT_URL, origin="dfe")
    actions.sort(
        key=lambda action: (
            -int(action["origin"] == "dfe" and action["confidence"] >= 2),
            -action["confidence"],
            action["url"],
            normalize_text(action["label"]),
        )
    )
    action_signals = sorted(f"action:{action['url']}:{normalize_text(action['label'])}" for action in actions)
    opening_actions = [action for action in actions if action["confidence"] >= 2]
    opening_signal = bool(opening_actions) and not registration_closed
    signature_input = f"{title}|{date}|{paragraph}|closed={registration_closed}|actions={'|'.join(action_signals)}"
    headline = "APPLICATION OPENING — Værnedamsvej" if opening_signal else f"{title} — {date}"
    direct_url = opening_actions[0]["url"] if opening_signal else PROJECT_STATUS_URL
    event = _readiness_event(
        "readiness:vaernedamsvej",
        "Den Franske Skole/Værnedamsvej",
        headline,
        paragraph,
        _signature(signature_input),
        direct_url,
        urgent=opening_signal,
        registration_closed=registration_closed,
    )
    event.update(
        {
            "kind": "application_opening" if opening_signal else "project_update",
            "signals": action_signals,
            "application_url": direct_url if opening_signal else DFE_PROJECT_URL,
            "urgent_headline": "APPLICATION OPENING — Værnedamsvej",
        }
    )
    return SourceSnapshot(source="Værnedamsvej", events=[event])
```

Keep `APPLICATION_HOSTS` exact and fixture-backed. If a future opening uses an external application provider, first capture and review the destination, then add only that exact HTTPS hostname with a focused test; page-controlled HTTP or unknown-host actions must never become an `@everyone` link.

- [ ] **Step 4: Run focused and complete tests**

Run:

```powershell
python -m unittest test_readiness_sources.VaernedamsvejSourceTests -v
python -m unittest discover -v
```

Expected: all tests pass; closed wording remains nonurgent and the application link is urgent.

- [ ] **Step 5: Commit the Værnedamsvej monitor**

```powershell
git add housing_sources/readiness.py test_readiness_sources.py
git commit -m "Add Vaernedamsvej application monitor"
```

### Task 13: Baseline Digest and Readiness Alert Pipeline

**Files:**
- Create: `test_alert_pipeline.py`
- Modify: `watcher.py:395-526,1181-1221`

- [ ] **Step 1: Add failing digest, seeding, catch-up, and readiness tests**

Create `test_alert_pipeline.py`:

```python
import unittest
from unittest.mock import patch

import watcher
from housing_sources import SourceSnapshot, SourceSpec


def rental(listing_id, address, price, source, status="Available", canonical_key=None):
    return {
        "id": listing_id,
        "status": status,
        "name": address,
        "price": {"amount": price},
        "location": {"formatted": address},
        "availableFrom": "See link for info",
        "url": f"https://example.test/{listing_id}",
        "source": source,
        "transaction_type": "rent",
        "price_period": "month",
        "canonical_key": canonical_key,
        "source_priority": 20,
    }


def readiness(event_id, source, signature, closed=False, urgent=False, signals=(), kind="project_update"):
    return {
        "id": event_id,
        "source": source,
        "headline": f"{source} current status",
        "description": "Inspect the official page for details.",
        "signature": signature,
        "url": "https://example.test/status",
        "urgent": urgent,
        "registration_closed": closed,
        "signals": list(signals),
        "kind": kind,
    }


class AlertPipelineTests(unittest.TestCase):
    def test_digest_contains_every_active_match_once_and_mentions_once(self):
        snapshots = [
            SourceSnapshot(
                "Findbolig",
                listings=[
                    rental("find:1", "Store Kongensgade 1, 1264 Kobenhavn K", 14000, "Findbolig"),
                    rental("find:2", "Osterbrogade 2, 2100 Kobenhavn O", 17000, "Findbolig"),
                    rental("find:reserved", "Osterbrogade 4, 2100 Kobenhavn O", 16000, "Findbolig", status="Reserved"),
                ],
            ),
            SourceSnapshot("RLE", events=[readiness("readiness:rle", "RLE", "empty")]),
        ]
        seen = {}
        payloads = []

        with patch.object(watcher, "WEBHOOK_URL", "https://discord.test/webhook"), patch.object(
            watcher, "DISCORD_MENTION_EVERYONE", "true"
        ), patch.object(watcher, "post_discord_payload", side_effect=lambda payload: payloads.append(payload) or True), patch.object(
            watcher, "save_seen_states"
        ):
            incomplete, delivery_failures = watcher.initialize_source_baselines(
                snapshots, {"Findbolig", "RLE"}, seen, max_chars=220
            )

        body = "\n".join(payload["content"] for payload in payloads)
        self.assertEqual(set(), incomplete)
        self.assertEqual(0, delivery_failures)
        self.assertEqual(1, body.count("@everyone"))
        self.assertEqual(1, body.count("Store Kongensgade 1"))
        self.assertEqual(1, body.count("Osterbrogade 2"))
        self.assertNotIn("Osterbrogade 4", body)
        self.assertIn("RLE current status", body)
        self.assertEqual("Reserved", seen["find:reserved"])
        self.assertEqual("complete", seen[watcher.baseline_state_key("Findbolig")])

    def test_source_is_not_seeded_until_all_of_its_chunks_succeed(self):
        snapshot = SourceSnapshot(
            "Findbolig",
            listings=[
                rental("find:1", "Store Kongensgade 1, 1264 Kobenhavn K", 14000, "Findbolig"),
                rental("find:2", "Osterbrogade 2, 2100 Kobenhavn O", 17000, "Findbolig"),
            ],
        )
        seen = {}
        chunks = watcher.build_baseline_digest_chunks([snapshot], max_chars=115)
        responses = [True] * len(chunks)
        responses[-1] = False
        with patch.object(watcher, "WEBHOOK_URL", "https://discord.test/webhook"), patch.object(
            watcher, "post_discord_payload", side_effect=responses
        ), patch.object(watcher, "save_seen_states"):
            incomplete, delivery_failures = watcher.initialize_source_baselines([snapshot], {"Findbolig"}, seen, max_chars=115)

        self.assertEqual({"Findbolig"}, incomplete)
        self.assertEqual(1, delivery_failures)
        self.assertNotIn("find:1", seen)
        self.assertNotIn(watcher.baseline_state_key("Findbolig"), seen)
        self.assertEqual("sent", seen[watcher.BASELINE_MENTION_STATE_KEY])
        self.assertTrue(any(key.startswith(watcher.BASELINE_CHUNK_STATE_PREFIX) for key in seen))

        retry_payloads = []
        with patch.object(watcher, "WEBHOOK_URL", "https://discord.test/webhook"), patch.object(
            watcher, "post_discord_payload", side_effect=lambda payload: retry_payloads.append(payload) or True
        ), patch.object(watcher, "save_seen_states"):
            incomplete, delivery_failures = watcher.initialize_source_baselines([snapshot], {"Findbolig"}, seen, max_chars=115)
        self.assertEqual(set(), incomplete)
        self.assertEqual(0, delivery_failures)
        self.assertEqual(1, len(retry_payloads))

    def test_later_first_success_is_a_source_specific_catch_up_digest(self):
        seen = {watcher.baseline_state_key("Findbolig"): "complete", watcher.BASELINE_MENTION_STATE_KEY: "sent"}
        snapshots = [
            SourceSnapshot("Findbolig", listings=[rental("find:1", "Kronprinsessegade 1, 1306 Kobenhavn K", 15000, "Findbolig")]),
            SourceSnapshot("RLE", events=[readiness("readiness:rle", "RLE", "empty")]),
        ]
        payloads = []
        with patch.object(watcher, "WEBHOOK_URL", "https://discord.test/webhook"), patch.object(
            watcher, "post_discord_payload", side_effect=lambda payload: payloads.append(payload) or True
        ), patch.object(watcher, "save_seen_states"):
            incomplete, delivery_failures = watcher.initialize_source_baselines(snapshots, {"Findbolig", "RLE"}, seen)

        self.assertEqual(set(), incomplete)
        self.assertEqual(0, delivery_failures)
        body = "\n".join(payload["content"] for payload in payloads)
        self.assertIn("RLE", body)
        self.assertNotIn("Kronprinsessegade", body)
        self.assertNotIn("@everyone", body)

    def test_cross_source_duplicate_uses_the_preferred_origin_in_digest(self):
        key = "rent:handelsvej 23 2 th 2450 kobenhavn sv"
        aggregator = rental("kobenhavn:1", "Handelsvej 23, 2. th., 2450 Kobenhavn SV", 12000, "Kobenhavn.dk", key)
        aggregator["source_priority"] = 40
        origin = rental("brikk:1", "Handelsvej 23, 2. th., 2450 Kobenhavn SV", 12000, "Origin", key)
        origin["source_priority"] = 10
        prepared = watcher.prepare_source_snapshots(
            [SourceSnapshot("Kobenhavn.dk", [aggregator]), SourceSnapshot("Origin", [origin])]
        )
        self.assertEqual([], prepared[0].listings)
        self.assertEqual(["brikk:1"], [item["id"] for item in prepared[1].listings])

    def test_readiness_signature_change_alerts_and_persists_only_after_success(self):
        event = readiness("readiness:cphhomes", "CPH Homes", "revision-2")
        seen = {watcher.readiness_state_key(event["id"]): {"signature": "revision-1", "registration_closed": False, "signals": []}}
        with patch.object(watcher, "send_readiness_notification", return_value=False) as send:
            sent, failures = watcher.process_readiness_events([event], seen)
        self.assertEqual((0, 1), (sent, failures))
        self.assertEqual("revision-1", seen[watcher.readiness_state_key(event["id"])]["signature"])
        send.assert_called_once()

    def test_registration_closed_to_open_transition_is_urgent(self):
        event = readiness("readiness:vaernedamsvej", "Den Franske Skole/Vaernedamsvej", "open", closed=False)
        event.update({"application_url": "https://example.test/apply", "urgent_headline": "APPLICATION OPENING — Værnedamsvej"})
        key = watcher.readiness_state_key(event["id"])
        seen = {key: {"signature": "closed", "registration_closed": True, "signals": []}}
        with patch.object(watcher, "send_readiness_notification", return_value=True) as send, patch.object(
            watcher, "save_seen_states"
        ):
            sent, failures = watcher.process_readiness_events([event], seen)
        self.assertEqual((1, 0), (sent, failures))
        self.assertTrue(send.call_args.kwargs["urgent"])
        self.assertEqual("application_opening", send.call_args.args[0]["kind"])
        self.assertEqual("https://example.test/apply", send.call_args.args[0]["url"])
        self.assertFalse(seen[key]["registration_closed"])

    def test_routine_readiness_has_no_ping_and_inspection_is_not_called_application_opening(self):
        captured = []
        inspection = readiness(
            "readiness:cphhomes:171",
            "CPH Homes",
            "changed",
            urgent=True,
            kind="inspection",
        )
        with patch.object(watcher, "WEBHOOK_URL", "https://discord.test/webhook"), patch.object(
            watcher, "post_discord_payload", side_effect=lambda payload: captured.append(payload) or True
        ):
            self.assertTrue(watcher.send_readiness_notification(inspection, urgent=True))
        self.assertNotIn("@everyone", captured[0]["content"])
        self.assertIn("Inspection needed", captured[0]["content"])
        self.assertNotIn("APPLICATION OPENING", captured[0]["content"])

    def test_existing_cph_signal_does_not_make_unrelated_revision_urgent(self):
        event = readiness(
            "readiness:cphhomes:171",
            "CPH Homes",
            "revision-2",
            signals=("term:husleje",),
            kind="inspection",
        )
        key = watcher.readiness_state_key(event["id"])
        seen = {key: {"signature": "revision-1", "registration_closed": False, "signals": ["term:husleje"]}}
        with patch.object(watcher, "send_readiness_notification", return_value=True) as send, patch.object(
            watcher, "save_seen_states"
        ):
            watcher.process_readiness_events([event], seen)
        self.assertFalse(send.call_args.kwargs["urgent"])

    def test_true_application_opening_mentions_once(self):
        captured = []
        opening = readiness(
            "readiness:vaernedamsvej",
            "Den Franske Skole/Vaernedamsvej",
            "open",
            urgent=True,
            kind="application_opening",
        )
        with patch.object(watcher, "WEBHOOK_URL", "https://discord.test/webhook"), patch.object(
            watcher, "DISCORD_MENTION_EVERYONE", "true"
        ), patch.object(watcher, "post_discord_payload", side_effect=lambda payload: captured.append(payload) or True):
            self.assertTrue(watcher.send_readiness_notification(opening, urgent=True))
        self.assertEqual(1, captured[0]["content"].count("@everyone"))
        self.assertIn("APPLICATION OPENING", captured[0]["content"])

    def test_parseable_rle_vacancy_sends_listing_only(self):
        listing = rental("rle:home", "Nørrebrogade 10, 2200 København N", 17500, "RLE")
        snapshots = [SourceSnapshot("RLE", listings=[listing], events=[])]
        specs = [SourceSpec("RLE", "ten_minute", lambda: snapshots[0])]
        seen = {watcher.baseline_state_key("RLE"): "complete"}
        with patch.object(watcher, "send_discord_notification", return_value=True) as home_alert, patch.object(
            watcher, "send_readiness_notification"
        ) as readiness_alert, patch.object(watcher, "save_seen_states"):
            sent, failures, incomplete = watcher.process_source_snapshots(snapshots, specs, seen)
        self.assertEqual((1, 0, set()), (sent, failures, incomplete))
        home_alert.assert_called_once()
        readiness_alert.assert_not_called()

    def test_canonical_state_suppresses_cross_run_source_replay_in_both_orders(self):
        key = "rent:handelsvej 23 2 th 2450 kobenhavn sv"
        origin = rental("origin:1", "Handelsvej 23, 2. th., 2450 Kobenhavn SV", 12000, "Origin", key)
        aggregator = rental("kobenhavn:1", "Handelsvej 23, 2. th., 2450 Kobenhavn SV", 12000, "Kobenhavn.dk", key)
        for first, second in ((origin, aggregator), (aggregator, origin)):
            seen = {}
            with self.subTest(first=first["source"]), patch.object(
                watcher, "send_discord_notification", return_value=True
            ) as send, patch.object(watcher, "save_seen_states"):
                self.assertEqual((1, 0), watcher.process_apartments([first], seen))
                self.assertEqual((0, 0), watcher.process_apartments([second], seen))
                send.assert_called_once()

    def test_new_reserved_is_seeded_silently_then_available_alerts(self):
        reserved = rental("lej:1", "Amagerbrogade 1, 2300 Kobenhavn S", 13000, "Lej", status="Reserved")
        available = dict(reserved, status="Available")
        seen = {}
        with patch.object(watcher, "send_discord_notification", return_value=True) as send, patch.object(
            watcher, "save_seen_states"
        ):
            self.assertEqual((0, 0), watcher.process_apartments([reserved], seen))
            send.assert_not_called()
            self.assertEqual((1, 0), watcher.process_apartments([available], seen))
            send.assert_called_once()

    def test_existing_raw_id_state_is_migrated_to_canonical_key_without_replay(self):
        listing = rental(
            "origin:legacy",
            "Nørrebrogade 1, 2200 København N",
            12000,
            "Origin",
            canonical_key="rent:norrebrogade 1 2200 kobenhavn n",
        )
        seen = {"origin:legacy": "Available"}
        with patch.object(watcher, "send_discord_notification") as send, patch.object(watcher, "save_seen_states"):
            self.assertEqual((0, 0), watcher.process_apartments([listing], seen))
        self.assertEqual("Available", seen[watcher.listing_state_key(listing)])
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the alert tests and verify the interfaces are missing**

Run:

```powershell
python -m unittest test_alert_pipeline -v
```

Expected: failures for the baseline and readiness pipeline functions.

- [ ] **Step 3: Add state keys, active-baseline selection, and global snapshot deduplication**

Import `deduplicate_listings` and add to `watcher.py` near the state helpers:

```python
BASELINE_STATE_PREFIX = "__meta__:baseline:"
BASELINE_CHUNK_STATE_PREFIX = "__meta__:baseline-chunk:"
READINESS_STATE_PREFIX = "__meta__:readiness:"
CANONICAL_LISTING_STATE_PREFIX = "__meta__:listing:"
BASELINE_MENTION_STATE_KEY = "__meta__:baseline-mention"


def baseline_state_key(source):
    return f"{BASELINE_STATE_PREFIX}{source}"


def readiness_state_key(event_id):
    return f"{READINESS_STATE_PREFIX}{event_id}"


def baseline_chunk_state_key(body):
    return f"{BASELINE_CHUNK_STATE_PREFIX}{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


def listing_state_key(listing):
    canonical_key = listing.get("canonical_key")
    if canonical_key:
        digest = hashlib.sha256(str(canonical_key).encode("utf-8")).hexdigest()
        return f"{CANONICAL_LISTING_STATE_PREFIX}{digest}"
    return listing.get("id")


def remember_listing_state(seen_states, listing):
    state_key = listing_state_key(listing)
    status = listing.get("status")
    if state_key:
        seen_states[state_key] = status
    if listing.get("id"):
        seen_states[listing["id"]] = status


def is_active_baseline_listing(listing):
    return normalize_text(listing.get("status")) in {"available", "ledig", "under opsigelse"}


def prepare_source_snapshots(snapshots):
    eligible = []
    for snapshot in snapshots:
        for listing in snapshot.listings:
            if listing_matches_policy(listing):
                eligible.append(listing)
    selected_object_ids = {id(listing) for listing in deduplicate_listings(eligible)}
    return [
        SourceSnapshot(
            source=snapshot.source,
            listings=[listing for listing in snapshot.listings if id(listing) in selected_object_ids],
            events=list(snapshot.events),
            diagnostics=list(snapshot.diagnostics),
        )
        for snapshot in snapshots
    ]


def _readiness_state(event):
    return {
        "signature": str(event.get("signature", "")),
        "registration_closed": bool(event.get("registration_closed", False)),
        "signals": sorted(set(event.get("signals") or [])),
    }
```

This keeps reserved records in state but excludes them from the baseline's active-home lines. `prepare_source_snapshots()` must run before both the baseline and ongoing alert paths.

Refactor `process_apartments()` to use the canonical state key when present, migrate old raw-ID state lazily, seed non-actionable states silently, and alert only when the current state becomes actionable:

```python
def process_apartments(apartments, seen_states):
    sent_notifications = 0
    notification_failures = 0
    for apartment in apartments:
        if not isinstance(apartment, dict):
            raise WatcherError("Apartment source returned an item in an unexpected format.")
        if not matches_general_listing_filters(apartment):
            continue
        apartment_id = apartment.get("id")
        status = apartment.get("status")
        if not apartment_id:
            continue
        state_key = listing_state_key(apartment)
        previous_status = seen_states.get(state_key)
        if previous_status is None and state_key != apartment_id:
            previous_status = seen_states.get(apartment_id)
            if previous_status is not None:
                seen_states[state_key] = previous_status
                save_seen_states(seen_states)
        actionable = is_active_baseline_listing(apartment)

        if previous_status == "unknown" or (previous_status is None and not actionable):
            remember_listing_state(seen_states, apartment)
            save_seen_states(seen_states)
            continue
        if previous_status == status:
            continue
        if not actionable:
            remember_listing_state(seen_states, apartment)
            save_seen_states(seen_states)
            continue

        reason = "New apartment found" if previous_status is None else f"Status changed ({previous_status} -> {status})"
        print(f"{reason}: {safe_console_text(apartment.get('name'))} ({apartment_id})")
        if send_discord_notification(apartment):
            remember_listing_state(seen_states, apartment)
            save_seen_states(seen_states)
            sent_notifications += 1
        else:
            notification_failures += 1
    return sent_notifications, notification_failures
```

- [ ] **Step 4: Implement compact chunks with source-level delivery accounting**

Add to `watcher.py`:

```python
def _compact_listing_line(listing):
    name = str(listing.get("name") or (listing.get("location") or {}).get("formatted") or "Unknown home")
    price = format_price_for_display(
        (listing.get("price") or {}).get("amount"), listing.get("price_period", "month")
    )
    status = str(listing.get("status") or "unknown")
    return f"- [{name}]({listing.get('url')}) — {price} — {status}"


def _snapshot_digest_lines(snapshot):
    active = [listing for listing in snapshot.listings if is_active_baseline_listing(listing)]
    lines = [f"**{snapshot.source}: {len(active)} active match(es)**"]
    if active:
        lines.extend(_compact_listing_line(listing) for listing in active)
    else:
        lines.append("- No active current matches.")
    for event in snapshot.events:
        headline = event.get("baseline_headline") or event.get("headline") or "Monitoring ready"
        lines.append(f"- Status: [{headline}]({event.get('url')})")
    return lines


def build_baseline_digest_chunks(snapshots, max_chars=1600):
    chunks = []
    current_lines = []
    current_sources = set()

    def flush_current():
        nonlocal current_lines, current_sources
        if current_lines:
            chunks.append({"body": "\n".join(current_lines), "sources": frozenset(current_sources)})
            current_lines = []
            current_sources = set()

    for snapshot in snapshots:
        source_lines = [line if len(line) <= max_chars else line[: max_chars - 1] + "…" for line in _snapshot_digest_lines(snapshot)]
        source_body = "\n".join(source_lines)
        if len(source_body) <= max_chars:
            projected = "\n".join(current_lines + source_lines)
            if current_lines and len(projected) > max_chars:
                flush_current()
            current_lines.extend(source_lines)
            current_sources.add(snapshot.source)
            continue

        flush_current()
        source_chunk_lines = []
        for line in source_lines:
            if source_chunk_lines and len("\n".join(source_chunk_lines + [line])) > max_chars:
                chunks.append({"body": "\n".join(source_chunk_lines), "sources": frozenset({snapshot.source})})
                source_chunk_lines = []
            source_chunk_lines.append(line)
        if source_chunk_lines:
            chunks.append({"body": "\n".join(source_chunk_lines), "sources": frozenset({snapshot.source})})
    flush_current()
    return chunks


def _baseline_payload(body, include_mention):
    mention, allowed_mentions = build_discord_mention() if include_mention else (None, None)
    prefix = f"{mention} " if mention else ""
    payload = {"content": f"{prefix}:house: **Housing tracker baseline**\n{body}"}
    if allowed_mentions:
        payload["allowed_mentions"] = allowed_mentions
    return payload


def initialize_source_baselines(snapshots, baseline_source_names, seen_states, max_chars=1600):
    pending = [
        snapshot
        for snapshot in snapshots
        if snapshot.source in baseline_source_names and seen_states.get(baseline_state_key(snapshot.source)) != "complete"
    ]
    if not pending:
        return set(), 0
    if not WEBHOOK_URL:
        print("Webhook URL not found. Baselines remain pending.")
        return {snapshot.source for snapshot in pending}, 1

    source_succeeded = {snapshot.source: True for snapshot in pending}
    delivery_failures = 0
    include_mention = seen_states.get(BASELINE_MENTION_STATE_KEY) != "sent"
    for chunk in build_baseline_digest_chunks(pending, max_chars=max_chars):
        chunk_key = baseline_chunk_state_key(chunk["body"])
        delivered = seen_states.get(chunk_key) == "sent"
        if not delivered:
            delivered = post_discord_payload(_baseline_payload(chunk["body"], include_mention))
            if delivered:
                seen_states[chunk_key] = "sent"
                save_seen_states(seen_states)
        if delivered and include_mention:
            seen_states[BASELINE_MENTION_STATE_KEY] = "sent"
            save_seen_states(seen_states)
            include_mention = False
        if not delivered:
            delivery_failures += 1
            for source in chunk["sources"]:
                source_succeeded[source] = False

    incomplete = set()
    changed = False
    for snapshot in pending:
        if not source_succeeded[snapshot.source]:
            incomplete.add(snapshot.source)
            continue
        for listing in snapshot.listings:
            if listing.get("id"):
                remember_listing_state(seen_states, listing)
        for event in snapshot.events:
            if event.get("id"):
                seen_states[readiness_state_key(event["id"])] = _readiness_state(event)
        seen_states[baseline_state_key(snapshot.source)] = "complete"
        changed = True
    if changed:
        save_seen_states(seen_states)
    return incomplete, delivery_failures
```

Import `hashlib` in `watcher.py`. The mention marker and each successful deterministic chunk hash are saved immediately. A retry skips already delivered chunks. A source marker and all of that source's listing/event states are saved only when every chunk containing that source succeeded.

- [ ] **Step 5: Implement readiness payloads and change processing**

Add to `watcher.py`:

```python
def send_readiness_notification(event, urgent=False):
    if not WEBHOOK_URL:
        print(f"Webhook URL not found. Readiness event remains pending: {safe_console_text(event.get('headline'))}")
        return False
    kind = event.get("kind", "project_update")
    is_application_opening = kind == "application_opening"
    mention, allowed_mentions = build_discord_mention() if is_application_opening else (None, None)
    label = {
        "application_opening": "APPLICATION OPENING",
        "inspection": "Inspection needed",
        "project_update": "Project update",
    }.get(kind, "Housing source update")
    payload = {
        "content": f"{mention + ' ' if mention else ''}:rotating_light: **{label} — {event.get('source', 'Source')}**",
        "embeds": [
            {
                "title": str(event.get("headline") or label),
                "url": event.get("url"),
                "description": str(event.get("description") or "Inspect the official page.")[:4000],
                "color": 15158332 if urgent else 15844367,
            }
        ],
    }
    if allowed_mentions:
        payload["allowed_mentions"] = allowed_mentions
    return post_discord_payload(payload)


def process_readiness_events(events, seen_states):
    sent = 0
    failures = 0
    for event in events:
        event_id = event.get("id")
        signature = str(event.get("signature") or "")
        if not event_id or not signature:
            raise WatcherError("Readiness event is missing an ID or signature.")
        key = readiness_state_key(event_id)
        previous = seen_states.get(key)
        previous_signature = previous.get("signature") if isinstance(previous, dict) else previous
        if previous_signature == signature:
            continue
        previous_closed = bool(previous.get("registration_closed")) if isinstance(previous, dict) else False
        previous_signals = set(previous.get("signals") or []) if isinstance(previous, dict) else set()
        current_signals = set(event.get("signals") or [])
        registration_opened = previous_closed and not event.get("registration_closed", False)
        urgent = bool(
            event.get("urgent")
            or registration_opened
            or (current_signals - previous_signals)
        )
        alert_event = dict(event)
        if registration_opened:
            alert_event["kind"] = "application_opening"
            alert_event["headline"] = event.get("urgent_headline") or "APPLICATION OPENING"
            alert_event["url"] = event.get("application_url") or event.get("url")
        if previous is not None and event.get("change_headline"):
            alert_event["headline"] = event["change_headline"]
        if send_readiness_notification(alert_event, urgent=urgent):
            seen_states[key] = _readiness_state(event)
            save_seen_states(seen_states)
            sent += 1
        else:
            failures += 1
    return sent, failures
```

- [ ] **Step 6: Run the focused and complete suites**

Run:

```powershell
python -m unittest test_alert_pipeline -v
python -m unittest discover -v
```

Expected: all tests pass. No test makes a network request or posts to Discord.

- [ ] **Step 7: Commit the alert pipeline**

```powershell
git add watcher.py test_alert_pipeline.py
git commit -m "Add compact Discord baselines and readiness alerts"
```

### Task 14: Cadence-Aware Source Registry and Orchestration

**Files:**
- Create: `test_source_scheduler.py`
- Modify: `watcher.py:1-35,1117-1314`
- Modify: `test_watcher.py:178-188,422-449`

- [ ] **Step 1: Add failing cadence and failure-isolation tests**

Create `test_source_scheduler.py`:

```python
import unittest
from unittest.mock import patch

import watcher
from housing_sources import SourceSnapshot, SourceSpec


class SourceSchedulerTests(unittest.TestCase):
    def test_registry_assigns_every_new_source_to_the_approved_cadence(self):
        cadences = {spec.name: spec.cadence for spec in watcher.make_source_registry()}
        self.assertEqual("fast", cadences["Findbolig"])
        self.assertEqual("fast", cadences["Lejeboligmægleren"])
        self.assertEqual("fast", cadences["Norhjem"])
        for source in ("Taurus", "Brikk", "Kobenhavn.dk", "RLE", "Værnedamsvej"):
            self.assertEqual("ten_minute", cadences[source])
        self.assertEqual("thirty_minute", cadences["CPH Homes"])

    def test_fast_runs_each_cycle_but_fixed_source_waits_until_due(self):
        calls = []
        registry = [
            SourceSpec("Fast", "fast", lambda: calls.append("fast") or SourceSnapshot("Fast"), baseline=False),
            SourceSpec("Ten", "ten_minute", lambda: calls.append("ten") or SourceSnapshot("Ten")),
        ]
        due = {}
        first, first_success = watcher.fetch_due_sources(registry, now=100.0, next_due=due)
        second, second_success = watcher.fetch_due_sources(registry, now=101.0, next_due=due)
        self.assertEqual(["fast", "ten", "fast"], calls)
        self.assertEqual({"Fast", "Ten"}, first_success)
        self.assertEqual({"Fast"}, second_success)
        self.assertEqual(["Fast", "Ten"], [snapshot.source for snapshot in first])
        self.assertEqual(["Fast"], [snapshot.source for snapshot in second])

    def test_failure_is_isolated_and_zero_results_count_as_success(self):
        def fail():
            raise RuntimeError("source unavailable")

        registry = [
            SourceSpec("Broken", "ten_minute", fail),
            SourceSpec("Empty", "ten_minute", lambda: SourceSnapshot("Empty")),
        ]
        snapshots, succeeded = watcher.fetch_due_sources(registry, now=0.0, next_due={})
        self.assertEqual(["Empty"], [snapshot.source for snapshot in snapshots])
        self.assertEqual({"Empty"}, succeeded)

    def test_snapshot_source_must_match_registry_name(self):
        registry = [SourceSpec("Expected", "fast", lambda: SourceSnapshot("Wrong"))]
        snapshots, succeeded = watcher.fetch_due_sources(registry, now=0.0, next_due={})
        self.assertEqual([], snapshots)
        self.assertEqual(set(), succeeded)

    def test_baseline_runs_before_individual_alerts(self):
        listing = {
            "id": "find:1",
            "status": "Available",
            "name": "Store Kongensgade 1, 1264 Kobenhavn K",
            "price": {"amount": 14000},
            "location": {"formatted": "Store Kongensgade 1, 1264 Kobenhavn K"},
            "url": "https://example.test/find:1",
            "source": "Findbolig",
            "transaction_type": "rent",
        }
        snapshots = [SourceSnapshot("Findbolig", [listing])]
        specs = [SourceSpec("Findbolig", "fast", lambda: snapshots[0])]
        seen = {}
        with patch.object(watcher, "initialize_source_baselines", side_effect=lambda _s, _n, state: state.update({"find:1": "Available", watcher.baseline_state_key("Findbolig"): "complete"}) or (set(), 0)) as baseline, patch.object(
            watcher, "send_discord_notification"
        ) as individual:
            sent, failures, incomplete = watcher.process_source_snapshots(snapshots, specs, seen)
        self.assertEqual((0, 0, set()), (sent, failures, incomplete))
        baseline.assert_called_once()
        individual.assert_not_called()

    def test_akf_uses_logical_baseline_without_replaying_other_propstep_homes(self):
        normal = {
            "id": "propstep:normal",
            "status": "Available",
            "name": "Nørrebrogade 1, 2200 København N",
            "price": {"amount": 12000},
            "location": {"formatted": "Nørrebrogade 1, 2200 København N"},
            "url": "https://propstep.test/normal",
            "source": "Propstep",
            "transaction_type": "rent",
        }
        akf = {**normal, "id": "propstep:akf", "name": "Nørrebrogade 2, 2200 København N", "location": {"formatted": "Nørrebrogade 2, 2200 København N"}, "url": "https://propstep.test/akf", "source": "AKF via Propstep"}
        snapshots = [SourceSnapshot("Propstep", [normal, akf])]
        specs = [SourceSpec("Propstep", "fast", lambda: snapshots[0], baseline=False)]
        seen = {"propstep:normal": "Available"}
        payloads = []
        with patch.object(watcher, "WEBHOOK_URL", "https://discord.test/webhook"), patch.object(
            watcher, "post_discord_payload", side_effect=lambda payload: payloads.append(payload) or True
        ), patch.object(watcher, "send_discord_notification") as individual, patch.object(watcher, "save_seen_states"):
            sent, failures, incomplete = watcher.process_source_snapshots(snapshots, specs, seen)
        body = "\n".join(payload["content"] for payload in payloads)
        self.assertEqual((0, 0, set()), (sent, failures, incomplete))
        self.assertIn("Nørrebrogade 2", body)
        self.assertNotIn("Nørrebrogade 1", body)
        self.assertEqual("complete", seen[watcher.baseline_state_key("AKF via Propstep")])
        individual.assert_not_called()

    def test_failed_baseline_is_counted_and_suppresses_individual_flood(self):
        listing = {
            "id": "find:1",
            "status": "Available",
            "name": "Store Kongensgade 1, 1264 København K",
            "price": {"amount": 14000},
            "location": {"formatted": "Store Kongensgade 1, 1264 København K"},
            "url": "https://example.test/find:1",
            "source": "Findbolig",
            "transaction_type": "rent",
        }
        snapshots = [SourceSnapshot("Findbolig", [listing])]
        specs = [SourceSpec("Findbolig", "fast", lambda: snapshots[0])]
        with patch.object(watcher, "initialize_source_baselines", return_value=({"Findbolig"}, 1)), patch.object(
            watcher, "send_discord_notification"
        ) as individual:
            sent, failures, incomplete = watcher.process_source_snapshots(snapshots, specs, {})
        self.assertEqual((0, 1, {"Findbolig"}), (sent, failures, incomplete))
        individual.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Replace obsolete fast/slow grouping assertions**

In `test_watcher.py`, remove the tests that assert two hard-coded source-name groups. Keep the CEJ rate-limit behavior test, but assert through `fetch_due_sources()` that a CEJ failure does not suppress a succeeding source. Scheduling itself now belongs to `test_source_scheduler.py`.

- [ ] **Step 3: Run scheduler tests and verify the missing registry functions**

Run:

```powershell
python -m unittest test_source_scheduler -v
```

Expected: failures for `fetch_due_sources()` and `process_source_snapshots()`.

- [ ] **Step 4: Add adapter imports, fixed intervals, and the source registry**

Add these imports to `watcher.py`:

```python
from housing_policy import deduplicate_listings, listing_matches_policy, normalize_text
from housing_sources import SourceContractError, SourceSnapshot, SourceSpec
from housing_sources.brikk import fetch_brikk
from housing_sources.findbolig import fetch_findbolig, make_findbolig_transport
from housing_sources.kobenhavn_dk import fetch_kobenhavn
from housing_sources.landlords import fetch_lejeboligmaegleren, fetch_norhjem, fetch_taurus
from housing_sources.readiness import fetch_cphomes, fetch_rle, fetch_vaernedamsvej
```

Add the thirty-minute interval beside the existing slow-source interval:

```python
READINESS_SOURCE_INTERVAL_SECONDS = max(
    300, read_non_negative_int_env("WATCHER_READINESS_SOURCE_INTERVAL_SECONDS", 1800)
)


def cadence_seconds(cadence):
    if cadence == "ten_minute":
        return SLOW_SOURCE_INTERVAL_SECONDS
    if cadence == "thirty_minute":
        return READINESS_SOURCE_INTERVAL_SECONDS
    if cadence == "fast":
        return 0
    raise ValueError(f"Unknown source cadence: {cadence}")
```

Add a form-encoded JSON helper beside `post_json()`:

```python
def post_form_json(url, payload):
    body = urlencode(payload, doseq=True).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_findbolig_live():
    fetch_text, post_search = make_findbolig_transport(HEADERS)
    return fetch_findbolig(fetch_text, post_search)
```

Import `urlencode` from `urllib.parse` in `watcher.py`.

Add the registry after all source fetch functions are defined:

```python
def make_source_registry():
    return [
        SourceSpec("CEJ", "fast", lambda: SourceSnapshot("CEJ", fetch_cej_apartments()), baseline=False),
        SourceSpec("City Apartment", "fast", lambda: SourceSnapshot("City Apartment", fetch_city_apartments()), baseline=False),
        SourceSpec("Propstep", "fast", lambda: SourceSnapshot("Propstep", fetch_propstep_apartments()), baseline=False),
        SourceSpec("Sweet Homes", "fast", lambda: SourceSnapshot("Sweet Homes", fetch_sweet_homes_apartments()), baseline=False),
        SourceSpec("Findbolig", "fast", fetch_findbolig_live),
        SourceSpec("Lejeboligmægleren", "fast", lambda: fetch_lejeboligmaegleren(post_json, fetch_json)),
        SourceSpec("Norhjem", "fast", lambda: fetch_norhjem(post_form_json, fetch_url_text)),
        SourceSpec("Capital Bolig", "ten_minute", lambda: SourceSnapshot("Capital Bolig", fetch_capitalbolig_apartments()), baseline=False),
        SourceSpec("Juli Living", "ten_minute", lambda: SourceSnapshot("Juli Living", fetch_juliliving_apartments()), baseline=False),
        SourceSpec("C.W. Obel", "ten_minute", lambda: SourceSnapshot("C.W. Obel", fetch_cwobel_apartments()), baseline=False),
        SourceSpec("Taurus", "ten_minute", lambda: fetch_taurus(fetch_url_text)),
        SourceSpec("Brikk", "ten_minute", lambda: fetch_brikk(fetch_url_text)),
        SourceSpec("Kobenhavn.dk", "ten_minute", lambda: fetch_kobenhavn(fetch_url_text)),
        SourceSpec("RLE", "ten_minute", lambda: fetch_rle(fetch_json)),
        SourceSpec("Værnedamsvej", "ten_minute", lambda: fetch_vaernedamsvej(fetch_url_text)),
        SourceSpec("CPH Homes", "thirty_minute", lambda: fetch_cphomes(fetch_url_text)),
    ]
```

AKF is intentionally absent from the network registry: Task 9 classifies it inside the one existing Propstep response, so no second HTTP request or duplicate ID is created. The orchestration code below derives a logical `AKF via Propstep` baseline snapshot from that successful response.

- [ ] **Step 5: Implement independent due-time fetching and snapshot processing**

Add to `watcher.py`:

```python
def fetch_due_sources(registry, now, next_due):
    snapshots = []
    succeeded = set()
    for spec in registry:
        due = spec.cadence == "fast" or now >= next_due.get(spec.name, 0.0)
        if not due:
            continue
        try:
            snapshot = spec.fetch()
            if not isinstance(snapshot, SourceSnapshot):
                raise SourceContractError(f"{spec.name} did not return a SourceSnapshot")
            if snapshot.source != spec.name:
                raise SourceContractError(
                    f"{spec.name} returned a snapshot labelled {snapshot.source}"
                )
            snapshots.append(snapshot)
            succeeded.add(spec.name)
        except Exception as exc:
            print(f"[{spec.name}] source fetch failed: {safe_console_text(exc)}")
        finally:
            if spec.cadence != "fast":
                next_due[spec.name] = now + cadence_seconds(spec.cadence)
    return snapshots, succeeded


LOGICAL_BASELINE_SUBSOURCES = {"Propstep": ("AKF via Propstep",)}


def build_logical_baseline_snapshots(snapshots, registry):
    baseline_names = {spec.name for spec in registry if spec.baseline}
    logical = [snapshot for snapshot in snapshots if snapshot.source in baseline_names]
    for snapshot in snapshots:
        for source_label in LOGICAL_BASELINE_SUBSOURCES.get(snapshot.source, ()):
            logical.append(
                SourceSnapshot(
                    source_label,
                    listings=[listing for listing in snapshot.listings if listing.get("source") == source_label],
                )
            )
    return logical


def process_source_snapshots(snapshots, registry, seen_states):
    prepared = prepare_source_snapshots(snapshots)
    logical_baselines = build_logical_baseline_snapshots(prepared, registry)
    baseline_sources = {snapshot.source for snapshot in logical_baselines}
    incomplete, baseline_failures = initialize_source_baselines(logical_baselines, baseline_sources, seen_states)
    ready = [
        SourceSnapshot(
            snapshot.source,
            listings=[listing for listing in snapshot.listings if listing.get("source") not in incomplete],
            events=list(snapshot.events),
            diagnostics=list(snapshot.diagnostics),
        )
        for snapshot in prepared
        if snapshot.source not in incomplete
    ]
    listings = [listing for snapshot in ready for listing in snapshot.listings]
    events = [event for snapshot in ready for event in snapshot.events]
    listing_sent, listing_failures = process_apartments(listings, seen_states)
    event_sent, event_failures = process_readiness_events(events, seen_states)
    return listing_sent + event_sent, baseline_failures + listing_failures + event_failures, incomplete
```

`succeeded` deliberately includes a contract-valid empty snapshot, while a parser or transport exception produces no snapshot. This is how the baseline distinguishes “zero current matches” from a failed source.

- [ ] **Step 6: Replace one-shot and adaptive orchestration with the registry**

Change `fetch_apartments()` to fetch each registered source once without invoking the alert pipeline:

```python
def fetch_apartments():
    registry = make_source_registry()
    snapshots, _succeeded = fetch_due_sources(registry, now=0.0, next_due={})
    prepared = prepare_source_snapshots(snapshots)
    return [listing for snapshot in prepared for listing in snapshot.listings]
```

Change `run_check()` to construct the registry, fetch once, and call `process_source_snapshots()`. In `run_adaptive_continuous_mode()`, replace `next_slow_fetch_monotonic` with `next_due = {}`, construct the registry once before the loop, and replace both hard-coded fetch groups with:

```python
now = time.monotonic()
snapshots, succeeded = fetch_due_sources(registry, now=now, next_due=next_due)
print(
    f"Fetched {sum(len(snapshot.listings) for snapshot in snapshots)} listings "
    f"and {sum(len(snapshot.events) for snapshot in snapshots)} readiness states "
    f"from {len(succeeded)} successful source(s)."
)
sent, failures, incomplete = process_source_snapshots(snapshots, registry, seen_states)
total_notification_failures += failures
if incomplete:
    print(f"Pending first-success baselines: {', '.join(sorted(incomplete))}")
```

Keep the existing adaptive sleep calculation and runtime deadline. Delete `fetch_fast_source_apartments()`, `fetch_slow_source_apartments()`, and the single shared next-slow timestamp after all callers and tests have moved to the registry.

- [ ] **Step 7: Run focused and complete tests**

Run:

```powershell
python -m unittest test_source_scheduler test_alert_pipeline test_watcher -v
python -m unittest discover -v
```

Expected: all tests pass, including the old state-file compatibility and Discord retry tests.

- [ ] **Step 8: Commit registry orchestration**

```powershell
git add watcher.py test_watcher.py test_source_scheduler.py
git commit -m "Schedule housing sources independently"
```

### Task 15: Documentation, CI Checks, and Read-Only Acceptance Probe

**Files:**
- Modify: `README.md`
- Modify: `.github/workflows/watcher.yml`
- Create: `docs/manual-contact-emails.md`
- Create: `docs/latest-source-scan.md`

- [ ] **Step 1: Document exact filters, source requirements, and alert semantics**

Add a source matrix to `README.md` containing these columns: source, content type, cadence, accepted eligibility, membership/age rule, and observed qualifying price range. The rows must state:

- Findbolig accepts only exact `Københavns Ejendomme` residence records and rejects pension, project, property, and waitlist records.
- Brikk accepts active andelsboliger strictly below `2,800,000 kr.`; ordinary cooperative-association buyer approval is disclosed, but it is not treated as a pension or age membership gate.
- Kobenhavn.dk is discovery-only, uses a strict `<15,000 kr.` rental cap and `<2,800,000 kr.` andel cap, and never alerts without origin verification.
- Taurus, Lejeboligmægleren, Norhjem, and public AKF/Propstep records use the `≤18,000 kr.` rental cap. Student, youth, senior, pension, and membership-restricted records are rejected. AKF's Waitly list is not queried.
- RLE creates homes only from structured residential blocks; commercial blocks are rejected. Ambiguous changes are readiness notices.
- CPH Homes and Værnedamsvej are readiness monitors, not confirmed-vacancy feeds.
- The agreed postcode set is `1000-2000, 2100, 2150, 2200, 2300, 2400, 2450`; explicitly list Valby `2500`, Brønshøj `2700`, Vanløse `2720`, and Kastrup `2770` as excluded.

For “observed qualifying price range,” run the live probe below and record the minimum and maximum from the accepted records for each listing source with the actual probe date. Write `none active in this scan` when a source has zero verified matches; do not infer a range from stale, sold, restricted, or out-of-area cards.

Document the baseline behavior: newly added sources produce a compact digest with one mention, reserved records are state-seeded but omitted from active matches, failed sources get a later catch-up digest, and subsequent changes use individual alerts.

- [ ] **Step 2: Document configuration and state metadata**

Add `WATCHER_READINESS_SOURCE_INTERVAL_SECONDS` to the environment-variable section with default `1800` and floor `300`. Clarify that `WATCHER_SLOW_SOURCE_INTERVAL_SECONDS` drives the ten-minute group and that the adaptive fast tier remains time-of-day aware.

Document these reserved state namespaces without changing existing listing-state strings:

```text
__meta__:baseline:<source>
__meta__:baseline-mention
__meta__:baseline-chunk:<sha256>
__meta__:readiness:<event-id>
__meta__:listing:<canonical-key-sha256>
```

- [ ] **Step 3: Preserve the two manual contact handoffs without sending email**

Create `docs/manual-contact-emails.md` with the official recipient, requirements, and these concise Danish drafts:

```markdown
# Manuelle bolighenvendelser

## ØENS Ejendomsadministration

**Send til:** `lejer@oadv.dk`
**Krav:** fuldt navn, e-mail, telefon, maksimumsbudget, ønskede områder, minimumsstørrelse/-værelser og husdyr. Listen er ikke nummereret og giver ingen garanti. Oplysninger slettes efter 12 måneder; sæt en manuel fornyelsespåmindelse efter 11 måneder.

**Emne:** Interesseliste – lejebolig i København/Frederiksberg

> Hej ØENS
> Jeg vil gerne registreres på jeres interesseliste for en lejebolig.
> Fulde navn: [NAVN]
> E-mail: [E-MAIL]
> Telefon: [TELEFON]
> Maksimal husleje: 18.000 kr. pr. måned [inkl./ekskl. forbrug]
> Områder: København K, V, Ø, N, NV, S/Amager, SV, Nordhavn og Frederiksberg
> Minimum: [ANTAL VÆRELSER] og [MIN. M²]
> Husdyr: [INGEN / TYPE OG ANTAL]
> Venlig hilsen
> [NAVN]

## Ejendomskontoret

**Send til:** `udlejning@ejendomskontoret.dk`
**Vilkår:** Udlejningen er primært til virksomheder, ambassader og internationale organisationer. Kontrakten indgås normalt med organisationen, navngiver beboeren og er typisk tidsbegrænset til fem år. Der kræves normalt tre måneders depositum plus tre måneders forudbetalt leje; husdyr kræver særskilt tilladelse.

**Emne:** Boligsøgning – København/Frederiksberg

> Hej Frank Holm Hansen
> Jeg vil gerne høre, om I kan registrere mit boligbehov og kontakte mig ved et passende lejemål.
> Jeg søger som [PRIVATPERSON / MEDARBEJDER HOS VIRKSOMHED]. [VIRKSOMHEDENS NAVN OG CVR KAN VÆRE KONTRAKTPART / JEG VIL GERNE HØRE, OM I OGSÅ UDLEJER DIREKTE TIL PRIVATPERSONER].
> Jeg søger i København K, V, Ø, N, NV, S/Amager, SV, Nordhavn eller Frederiksberg til højst 18.000 kr. pr. måned. Mit minimum er [ANTAL VÆRELSER] og [MIN. M²], med ønsket indflytning [DATO/PERIODE]. Husdyr: [INGEN / TYPE OG ANTAL].
> Jeg har noteret mig vilkårene om sikkerhed, tidsbegrænsning og eventuel virksomhed som kontraktpart.
> Venlig hilsen
> [NAVN]
> [TELEFON]
```

Do not send either message and do not create a calendar reminder; both are explicit manual follow-ups.

- [ ] **Step 4: Expand the workflow's static and test verification**

Update `.github/workflows/watcher.yml` so its verification step runs:

```powershell
python -m py_compile watcher.py housing_policy.py housing_sources/__init__.py housing_sources/findbolig.py housing_sources/brikk.py housing_sources/kobenhavn_dk.py housing_sources/landlords.py housing_sources/readiness.py
python -m unittest discover -v
```

Do not add browser automation, credentials, or third-party packages.

- [ ] **Step 5: Run a read-only live parser probe with Discord disabled**

Run:

```powershell
$env:DISCORD_WEBHOOK_URL = ""
python -c "import time, watcher; registry=watcher.make_source_registry(); snapshots,ok=watcher.fetch_due_sources(registry,time.monotonic(),{}); print('successful:', ', '.join(sorted(ok))); [(print(s.source, 'accepted=', len(s.listings), 'events=', len(s.events), 'rejected/review=', len(s.diagnostics)), [print(' ACCEPT', x.get('transaction_type','rent'), x.get('name'), x.get('price',{}).get('amount'), x.get('url')) for x in s.listings], [print(' DIAGNOSTIC', d.get('outcome'), d.get('candidate_id'), d.get('reason'), d.get('origin_url')) for d in s.diagnostics]) for s in snapshots]"
```

Expected: the command performs only unauthenticated reads, never calls the alert pipeline, reports a contract-valid snapshot for every reachable source, and prints each accepted listing plus Kobenhavn.dk's suppressed, error, and `manual_review` diagnostics. The Kobenhavn.dk implementation-time origin manifest must have no unresolved current host; a `manual_review` result is acceptable only for a host that appeared after that captured release scan. Investigate any failed contract before recording its range; a failure is not equivalent to zero matches.

Create `docs/latest-source-scan.md` from this exact output with the actual timestamp, successful and failed source names, accepted counts, accepted listing address/price/URL rows, the captured Kobenhavn.dk origin-host manifest, verifier status for every host, and every diagnostic. This is the requested one-time scan evidence; never label an unsupported or ambiguous origin as active, and do not call the Kobenhavn.dk scan complete while a current manifest host lacks a verifier.

- [ ] **Step 6: Run the complete local acceptance suite**

Run:

```powershell
python -m py_compile watcher.py housing_policy.py housing_sources/__init__.py housing_sources/findbolig.py housing_sources/brikk.py housing_sources/kobenhavn_dk.py housing_sources/landlords.py housing_sources/readiness.py
python -m unittest discover -v
git diff --check
```

Expected: compilation succeeds, all tests pass, and `git diff --check` prints nothing.

- [ ] **Step 7: Commit documentation and workflow verification**

```powershell
git add README.md .github/workflows/watcher.yml docs/manual-contact-emails.md docs/latest-source-scan.md
git commit -m "Document expanded housing coverage"
```

- [ ] **Step 8: Perform final acceptance review**

Run:

```powershell
git status --short
git log --oneline -15
```

Expected: the worktree is clean, the task commits appear in order, and no implementation file remains untracked.
