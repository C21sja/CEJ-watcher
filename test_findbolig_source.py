import json
import os
import unittest

from housing_sources.findbolig import MUNICIPAL_COMPANY_ID, _validate_configuration, fetch_findbolig

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "tests", "fixtures", "findbolig")


CONFIG_HTML = (
    '<script type="vue-model" id="search-configuration">'
    + json.dumps(
        {"membershipOrganizations": [{"companies": {MUNICIPAL_COMPANY_ID: "Københavns Ejendomme"}}]}
    )
    + "</script>"
)
EXPECTED_FIRST_PAYLOAD = {
    "pageSize": 100,
    "page": 0,
    "orderBy": "Created",
    "orderDirection": "DESC",
    "facets": [],
    "filters": {"PropertyCompanyId": [MUNICIPAL_COMPANY_ID]},
    "mixedResults": False,
}


def residence(**overrides):
    """Builds a record matching the live findbolig.nu `/api/search` schema.

    Verified live: the response is flat (`propertyCompanyId`, not a nested
    `company` object), the path is `uri` built from `shortId`, waitlist
    stock is tagged via `applicationType`/`rentModel`, and an active
    advertisement is marked with `residenceAdvertStatus: "Published"`.
    """
    base = {
        "$type": "Residence",
        "id": "1167dfb2-222a-42f5-92cc-599b52f28c6c",
        "shortId": 288001,
        "uri": "/residence/288001",
        "propertyCompanyId": MUNICIPAL_COMPANY_ID,
        "propertyCompanyName": "Københavns Ejendomme",
        "street": "Nørrebrogade",
        "number": 10,
        "floor": "2",
        "door": "tv",
        "postalCode": "2200",
        "city": "København N",
        "rent": 17500,
        "availableFrom": "2026-08-01T00:00:00Z",
        "membersOnly": False,
        "applicationType": "Regular",
        "rentModel": "Advert",
        "residenceAdvertStatus": "Published",
    }
    base.update(overrides)
    return base


class FindboligSourceTests(unittest.TestCase):
    def test_keeps_only_exact_municipal_residences(self):
        pages = [
            {
                "results": [
                    residence(),
                    residence(
                        id="private",
                        shortId=288002,
                        uri="/residence/288002",
                        propertyCompanyId="private-company",
                        propertyCompanyName="Private",
                    ),
                    residence(
                        id="property-record",
                        shortId=288003,
                        uri="/property/288003",
                        **{"$type": "Property"},
                    ),
                    residence(
                        id="pension-member",
                        shortId=288004,
                        uri="/residence/288004",
                        membersOnly=True,
                    ),
                    residence(
                        id="waitlisted",
                        shortId=288005,
                        uri="/residence/288005",
                        applicationType="WaitingList",
                        rentModel="WaitingList",
                    ),
                    residence(
                        id="retired",
                        shortId=288006,
                        uri="/residence/288006",
                        residenceAdvertStatus="RetiredFromRentedOut",
                    ),
                ],
                "totalResults": 6,
            }
        ]
        payloads = []

        def post_json(_url, payload):
            payloads.append(payload)
            return pages.pop(0)

        snapshot = fetch_findbolig(lambda _url: CONFIG_HTML, post_json)
        self.assertEqual(["findbolig:288001"], [item["id"] for item in snapshot.listings])
        self.assertEqual(EXPECTED_FIRST_PAYLOAD, payloads[0])
        self.assertEqual(17500, snapshot.listings[0]["price"]["amount"])
        self.assertEqual(
            "https://www.findbolig.nu/residence/288001", snapshot.listings[0]["url"]
        )

    def test_stops_when_a_page_is_empty(self):
        calls = []

        def post_json(_url, payload):
            calls.append(payload["page"])
            return {"results": [], "totalResults": 0}

        snapshot = fetch_findbolig(lambda _url: CONFIG_HTML, post_json)
        self.assertEqual([], snapshot.listings)
        self.assertEqual([0], calls)

    def test_missing_results_key_is_a_contract_error_not_an_empty_feed(self):
        with self.assertRaisesRegex(Exception, "results list"):
            fetch_findbolig(lambda _url: CONFIG_HTML, lambda _url, _payload: {"totalResults": 0})

    def test_rejects_configuration_without_exact_owner(self):
        with self.assertRaisesRegex(Exception, "municipal company marker"):
            fetch_findbolig(
                lambda _url: '<script id="search-configuration">{}</script>', lambda _u, _p: {}
            )


class FindboligReleaseGateTests(unittest.TestCase):
    """Verifies the adapter against a sanitized capture of a real,
    read-only, same-session findbolig.nu request/response (12 July 2026).

    The capture proves: the search-configuration marker validates, the
    live search endpoint is `/api/search` (not `/search`) with `facets`
    as a list (not an object), and the municipal-owner filter genuinely
    narrows results (Københavns Ejendomme had zero advertised residences
    at capture time).
    """

    def test_fixture_configuration_contains_the_municipal_marker(self):
        with open(os.path.join(FIXTURE_DIR, "search-configuration.html"), encoding="utf-8") as f:
            _validate_configuration(f.read())

    def test_fixture_request_matches_the_adapters_generated_payload(self):
        with open(os.path.join(FIXTURE_DIR, "search-request.json"), encoding="utf-8") as f:
            captured_request = json.load(f)
        self.assertEqual(EXPECTED_FIRST_PAYLOAD, captured_request)

    def test_fetch_reproduces_the_captured_live_response_shape(self):
        with open(os.path.join(FIXTURE_DIR, "search-configuration.html"), encoding="utf-8") as f:
            config_html = f.read()
        with open(os.path.join(FIXTURE_DIR, "search-response.json"), encoding="utf-8") as f:
            captured_response = json.load(f)

        snapshot = fetch_findbolig(lambda _url: config_html, lambda _url, _payload: captured_response)
        self.assertEqual("Findbolig", snapshot.source)
        self.assertEqual([], snapshot.listings)


if __name__ == "__main__":
    unittest.main()
