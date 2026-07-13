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
                    "text": [
                        {
                            "children": [
                                {"text": "Vi har på nuværende tidspunkt ingen ledige ejendomme."}
                            ]
                        }
                    ],
                }
            ],
        }
        snapshot = parse_rle_document(document)
        self.assertEqual([], snapshot.listings)
        self.assertEqual("No residential vacancies", snapshot.events[0]["headline"])

    def test_parses_residential_block_and_rejects_commercial_block(self):
        document = {
            "content": [
                {
                    "_key": "home",
                    "_type": "vacancy",
                    "use": "bolig",
                    "status": "ledig",
                    "address": "Nørrebrogade 10",
                    "postalCode": 2200,
                    "city": "København N",
                    "monthlyRent": 17500,
                    "description": "Privat lejlighed",
                },
                {
                    "_key": "shop",
                    "_type": "vacancy",
                    "use": "erhverv",
                    "status": "ledig",
                    "address": "Østerbrogade 1",
                    "postalCode": 2100,
                    "city": "København Ø",
                    "monthlyRent": 10000,
                },
                {
                    "_key": "student",
                    "_type": "vacancy",
                    "use": "bolig",
                    "status": "ledig",
                    "address": "Nørrebrogade 12",
                    "postalCode": 2200,
                    "city": "København N",
                    "monthlyRent": 7000,
                    "eligibility": "Kun for studerende",
                },
            ]
        }
        snapshot = parse_rle_document(document)
        self.assertEqual(["rle:home"], [item["id"] for item in snapshot.listings])
        self.assertEqual([], snapshot.events)
        self.assertEqual(
            "rent:norrebrogade 10 2200 kobenhavn n", snapshot.listings[0]["canonical_key"]
        )

    def test_descriptive_portable_text_does_not_become_a_fake_vacancy(self):
        document = {
            "content": [
                {
                    "_key": "copy",
                    "text": [
                        {
                            "children": [
                                {
                                    "text": (
                                        "Vi ejer boliger på Nørrebrogade 10, 2200 "
                                        "København N til en værdi af 17.500 kr."
                                    )
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        snapshot = parse_rle_document(document)
        self.assertEqual([], snapshot.listings)
        self.assertEqual("RLE changed - inspect now", snapshot.events[0]["headline"])

    def test_unclassified_replacement_creates_inspection_event(self):
        snapshot = parse_rle_document(
            {"content": [{"_key": "changed", "text": "Nyt indhold offentliggjort"}]}
        )
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
