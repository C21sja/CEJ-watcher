import unittest
from unittest.mock import patch

import watcher


class LocationFilterTests(unittest.TestCase):
    def test_rejects_non_copenhagen_postcodes_even_if_in_1000_3999_range(self):
        self.assertFalse(
            watcher.matches_cej_location_and_price("Bakkegade 21, 2. 19, 3400 Hillerød", 6995)
        )

    def test_keeps_copenhagen_addresses(self):
        self.assertTrue(
            watcher.matches_cej_location_and_price("Rebslagervej 7B, 2. tv, 2400 København NV", 18000)
        )

    def test_rejects_prices_over_max(self):
        self.assertFalse(
            watcher.matches_cej_location_and_price("Rebslagervej 7B, 2. tv, 2400 København NV", 18001)
        )

    def test_rejects_excluded_cities(self):
        self.assertFalse(
            watcher.matches_cej_location_and_price("Hvidovrevej 10, 2650 Hvidovre", 12000)
        )

    def test_rejects_valby_and_vanlose(self):
        self.assertFalse(
            watcher.matches_cej_location_and_price("Toftegaards Alle 5, 2500 Valby", 12000)
        )
        self.assertFalse(
            watcher.matches_cej_location_and_price("Jernbane Alle 12, 2720 Vanlose", 12000)
        )


class GeneralFilterTests(unittest.TestCase):
    def test_rejects_general_listings_above_max_price(self):
        listing = {
            "price": {"amount": 19000},
            "location": {"formatted": "Sankt Annae Plads 1, 1250 Koebenhavn K"},
        }
        self.assertFalse(watcher.matches_general_listing_filters(listing))

    def test_rejects_general_listings_in_excluded_locations(self):
        listing = {
            "price": {"amount": 12000},
            "location": {"formatted": "Park Alle 10, 2605 Brondby"},
            "name": "Lejlighed taet paa Ballerup",
        }
        self.assertFalse(watcher.matches_general_listing_filters(listing))

    def test_accepts_general_listings_within_rules(self):
        listing = {
            "price": {"amount": 15000},
            "location": {"formatted": "Norrebrogade 1, 2200 Koebenhavn N"},
        }
        self.assertTrue(watcher.matches_general_listing_filters(listing))


class PropstepTests(unittest.TestCase):
    @patch("watcher.post_json")
    def test_uses_transaction_status_for_listing_status(self, mock_post_json):
        mock_post_json.return_value = {
            "searchResults": [
                {
                    "properties": [
                        {
                            "id": "listing-1",
                            "slug": "rebslagervej-7b-2-tv",
                            "name": "Rebslagervej 7B, 2. tv",
                            "status": 3,
                            "transactionStatus": 1,
                            "location": {
                                "address": "Rebslagervej 7B, 2. tv",
                                "postalcode": "2400",
                                "city": "København NV",
                            },
                            "transactionDetails": {
                                "price": 1800000,
                                "availableFrom": "2026-04-14T22:00:00.000+00:00",
                            },
                            "propertyDetails": {"size": 82, "rooms": 3},
                        }
                    ]
                }
            ],
            "totalProperties": 1,
        }

        apartments = watcher.fetch_propstep_apartments()

        self.assertEqual(1, len(apartments))
        self.assertEqual("Available", apartments[0]["status"])
        self.assertEqual(82, apartments[0]["size_sqm"])
        self.assertEqual(3, apartments[0]["rooms"])


class CapitalBoligTests(unittest.TestCase):
    @patch("watcher.fetch_url_text")
    @patch("watcher.fetch_json")
    def test_extracts_price_size_and_rooms_from_listing_page(self, mock_fetch_json, mock_fetch_url_text):
        mock_fetch_json.return_value = [
            {
                "id": 4103,
                "link": "https://capitalbolig.dk/bolig/anker-heegaards-gade-1a-4-th-1572-koebenhavn-v/",
                "title": {"rendered": "Anker Heegaards Gade 1A, 4 th., 1572 København V"},
            }
        ]
        mock_fetch_url_text.return_value = """
            <html>
                <h4>Overtagelsesdato</h4>
                <div class="fusion-text"><p>01/05/2026</p></div>
                <h4>Husleje</h4>
                <div class="fusion-text"><p>23.800 kr. pr. md.</p></div>
                <h4>Antal m2</h4>
                <div class="fusion-text"><p>107 kvm</p></div>
                <h4>Antal rum</h4>
                <div class="fusion-text"><p>3</p></div>
            </html>
        """

        apartments = watcher.fetch_capitalbolig_apartments()

        self.assertEqual(1, len(apartments))
        self.assertEqual(23800, apartments[0]["price"]["amount"])
        self.assertEqual(107, apartments[0]["size_sqm"])
        self.assertEqual(3, apartments[0]["rooms"])
        self.assertEqual("01/05/2026", apartments[0]["availableFrom"])


class DiscordNotificationTests(unittest.TestCase):
    @patch("watcher.post_discord_payload")
    @patch("watcher.build_discord_mention", return_value=("@everyone", {"parse": ["everyone"]}))
    @patch("watcher.WEBHOOK_URL", "https://discord.example/webhook")
    def test_embed_header_and_fields_show_source_and_property_details(self, _mock_mention, mock_post_discord_payload):
        captured = {}

        def capture(payload, max_attempts=5):
            captured["payload"] = payload
            return True

        mock_post_discord_payload.side_effect = capture

        listing = {
            "id": "propstep:listing-1",
            "name": "Rebslagervej 7B, 2. tv",
            "status": "Available",
            "price": {"amount": 20000},
            "location": {"formatted": "Rebslagervej 7B, 2. tv, 2400 København NV"},
            "availableFrom": "2026-04-15",
            "url": "https://propstep.com/da-DK/soeg?slug=rebslagervej-7b-2-tv",
            "source": "Propstep",
            "size_sqm": 82,
            "rooms": 3,
        }

        self.assertTrue(watcher.send_discord_notification(listing))

        embed = captured["payload"]["embeds"][0]
        field_names = [field["name"] for field in embed["fields"]]

        self.assertIn("Propstep", captured["payload"]["content"])
        self.assertIn("Propstep", embed["title"])
        self.assertIn("Area", field_names)
        self.assertIn("Rooms", field_names)

    @patch("watcher.post_discord_payload")
    @patch("watcher.build_discord_mention", return_value=("@everyone", {"parse": ["everyone"]}))
    @patch("watcher.WEBHOOK_URL", "https://discord.example/webhook")
    def test_unknown_price_does_not_append_currency_suffix(self, _mock_mention, mock_post_discord_payload):
        captured = {}

        def capture(payload, max_attempts=5):
            captured["payload"] = payload
            return True

        mock_post_discord_payload.side_effect = capture

        listing = {
            "id": "capital:4103",
            "name": "Anker Heegaards Gade 1A, 4 th., 1572 København V",
            "status": "Available",
            "price": {"amount": "Unknown"},
            "location": {"formatted": "Anker Heegaards Gade 1A, 4 th., 1572 København V"},
            "availableFrom": "See link for info",
            "url": "https://capitalbolig.dk/bolig/anker-heegaards-gade-1a-4-th-1572-koebenhavn-v/",
            "source": "Capital Bolig",
        }

        self.assertTrue(watcher.send_discord_notification(listing))

        embed = captured["payload"]["embeds"][0]
        price_field = next(field for field in embed["fields"] if field["name"] == "Price")

        self.assertEqual("Unknown", price_field["value"])


if __name__ == "__main__":
    unittest.main()
