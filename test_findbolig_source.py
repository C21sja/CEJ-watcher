import json
import unittest

from housing_sources.findbolig import MUNICIPAL_COMPANY_ID, fetch_findbolig


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
            fetch_findbolig(
                lambda _url: '<script id="search-configuration">{}</script>', lambda _u, _p: {}
            )


if __name__ == "__main__":
    unittest.main()
