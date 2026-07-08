import unittest
import urllib.error
from datetime import datetime, timezone
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


class RuntimeConfigTests(unittest.TestCase):
    def test_default_run_count_uses_continuous_adaptive_loop(self):
        # 0 is the sentinel meaning "use run_adaptive_continuous_mode()" rather
        # than the legacy fixed-count/fixed-interval single-shot mode.
        self.assertEqual(0, watcher.RUN_COUNT)


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


class CEJFetchTests(unittest.TestCase):
    def test_retries_cej_rate_limit_before_returning_items(self):
        rate_limit_error = urllib.error.HTTPError(
            watcher.API_URL,
            429,
            "Too Many Requests",
            {},
            None,
        )
        response_body = b'{"searchResponse": {"items": [{"id": "cej-1", "status": 1}]}}'

        class SuccessfulResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                return False

            def read(self):
                return response_body

        with patch("watcher.urllib.request.urlopen", side_effect=[rate_limit_error, SuccessfulResponse()]):
            with patch("watcher.time.sleep") as mock_sleep:
                apartments = watcher.fetch_cej_apartments(max_attempts=2, base_delay_seconds=1)

        self.assertEqual([{"id": "cej-1", "status": 1}], apartments)
        mock_sleep.assert_called_once_with(1)

    @patch("watcher.fetch_sweet_homes_apartments", return_value=[])
    @patch("watcher.fetch_propstep_apartments", return_value=[])
    @patch("watcher.fetch_cwobel_apartments", return_value=[])
    @patch("watcher.fetch_juliliving_apartments", return_value=[])
    @patch("watcher.fetch_capitalbolig_apartments", return_value=[])
    @patch("watcher.fetch_city_apartments", return_value=[])
    @patch("watcher.fetch_cej_apartments", side_effect=watcher.WatcherError("CEJ API rate limited after 3 attempts."))
    def test_fetch_apartments_skips_rate_limited_cej(
        self,
        _mock_fetch_cej,
        _mock_fetch_city,
        _mock_fetch_capital,
        _mock_fetch_juli,
        _mock_fetch_cwobel,
        _mock_fetch_propstep,
        _mock_fetch_sweet_homes,
    ):
        self.assertEqual([], watcher.fetch_apartments())


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


class CopenhagenTimeTests(unittest.TestCase):
    def test_winter_offset_is_utc_plus_one(self):
        utc = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(watcher.copenhagen_now(utc).hour, 13)

    def test_summer_offset_is_utc_plus_two(self):
        utc = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(watcher.copenhagen_now(utc).hour, 14)

    def test_dst_spring_forward_boundary(self):
        # EU summer time begins 2026-03-29 at 01:00 UTC.
        before = datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc)
        after = datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)
        self.assertEqual(watcher.copenhagen_now(before).hour, 1)   # 00:30 + 1h (CET)
        self.assertEqual(watcher.copenhagen_now(after).hour, 3)    # 01:30 + 2h (CEST)

    def test_dst_fall_back_boundary(self):
        # EU summer time ends 2026-10-25 at 01:00 UTC.
        before = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
        after = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
        self.assertEqual(watcher.copenhagen_now(before).hour, 2)   # 00:30 + 2h (CEST)
        self.assertEqual(watcher.copenhagen_now(after).hour, 2)    # 01:30 + 1h (CET)


class ClassifyPeriodTests(unittest.TestCase):
    @staticmethod
    def _monday(hour):
        return datetime(2026, 7, 6, hour, 0)  # a Monday

    @staticmethod
    def _saturday(hour):
        return datetime(2026, 7, 11, hour, 0)  # a Saturday

    def test_weekday_hot_window_matches_observed_cej_peak(self):
        # Live CEJ `lastPublishedDate` histogram (2026-07-08 snapshot) showed
        # ~62% of publish/status-change events landing 08:00-13:00 CPH.
        for hour in (8, 10, 12):
            self.assertEqual(watcher.classify_period(self._monday(hour)), "HOT")

    def test_weekday_warm_window_covers_afternoon_tail_and_ramp_up(self):
        # Covers both real Discord detections (Thu 16:40, Fri 15:52 CPH).
        for hour in (7, 13, 15, 16, 17):
            self.assertEqual(watcher.classify_period(self._monday(hour)), "WARM")

    def test_weekday_cool_and_cold_windows(self):
        for hour in (18, 20, 21):
            self.assertEqual(watcher.classify_period(self._monday(hour)), "COOL")
        for hour in (0, 3, 6, 22, 23):
            self.assertEqual(watcher.classify_period(self._monday(hour)), "COLD")

    def test_weekend_is_cool_by_day_cold_by_night(self):
        self.assertEqual(watcher.classify_period(self._saturday(12)), "COOL")
        self.assertEqual(watcher.classify_period(self._saturday(7)), "COLD")
        self.assertEqual(watcher.classify_period(self._saturday(23)), "COLD")


class PollIntervalTests(unittest.TestCase):
    def test_hot_window_uses_hot_interval(self):
        monday_peak = datetime(2026, 7, 6, 10, 0)
        self.assertEqual(
            watcher.get_poll_interval_seconds(monday_peak),
            watcher.POLL_INTERVALS["HOT"],
        )

    def test_night_uses_cold_interval(self):
        monday_night = datetime(2026, 7, 6, 3, 0)
        self.assertEqual(
            watcher.get_poll_interval_seconds(monday_night),
            watcher.POLL_INTERVALS["COLD"],
        )

    def test_intervals_speed_up_with_activity(self):
        i = watcher.POLL_INTERVALS
        self.assertLessEqual(i["HOT"], i["WARM"])
        self.assertLessEqual(i["WARM"], i["COOL"])
        self.assertLessEqual(i["COOL"], i["COLD"])

    def test_adaptive_disabled_falls_back_to_constant(self):
        original = watcher.ADAPTIVE_POLLING
        watcher.ADAPTIVE_POLLING = False
        try:
            monday_peak = datetime(2026, 7, 6, 10, 0)
            self.assertEqual(
                watcher.get_poll_interval_seconds(monday_peak),
                watcher.SLEEP_SECONDS,
            )
        finally:
            watcher.ADAPTIVE_POLLING = original

    def test_tiers_never_faster_than_ten_seconds(self):
        for interval in watcher.POLL_INTERVALS.values():
            self.assertGreaterEqual(interval, 10)


class CityApartmentAreaFilterTests(unittest.TestCase):
    def test_accepts_koebenhavn_k_by_postcode(self):
        self.assertTrue(watcher.is_city_apartment_target_area("Gothersgade 1 Post nr. 1123"))

    def test_accepts_vesterbro_by_postcode(self):
        self.assertTrue(watcher.is_city_apartment_target_area("Istedgade 5 Post nr. 1650"))

    def test_accepts_frederiksberg_by_postcode(self):
        self.assertTrue(watcher.is_city_apartment_target_area("Falkoner Alle 1 Post nr. 2000"))

    def test_accepts_oesterbro_by_postcode(self):
        self.assertTrue(watcher.is_city_apartment_target_area("Oesterbrogade 1 Post nr. 2100"))

    def test_accepts_amager_by_postcode(self):
        self.assertTrue(watcher.is_city_apartment_target_area("Amagerbrogade 1 Post nr. 2300"))
        self.assertTrue(watcher.is_city_apartment_target_area("Postnummer 2770 Kastrup"))

    def test_accepts_by_keyword_when_postcode_missing(self):
        self.assertTrue(watcher.is_city_apartment_target_area("Dejlig lejlighed paa Vesterbro"))

    def test_rejects_non_target_areas(self):
        self.assertFalse(watcher.is_city_apartment_target_area("Saxovej 75, Post nr. 5210 Odense"))
        self.assertFalse(watcher.is_city_apartment_target_area("Kildevej 12, Post nr. 2600 Glostrup"))
        self.assertFalse(watcher.is_city_apartment_target_area("Bronzebakken 66, Post nr. 3200 Helsinge"))
        self.assertFalse(watcher.is_city_apartment_target_area("Noerrebrogade 1, Post nr. 2200 Koebenhavn N"))


class CityApartmentParsingTests(unittest.TestCase):
    # A trimmed-down fixture mirroring the real page structure: an outer
    # non-listing <article> (the WordPress page shell) wrapping the whole
    # body, followed by sibling <article class="... cityapartments ...">
    # listing cards. A naive `<article>...</article>` regex swallows the
    # first real card into the page-shell match; the parser must not do that.
    SAMPLE_HTML = """
    <article class="post-1 page type-page ast-article-single">
      <p>Intro text about Copenhagen apartments.</p>
      <article id="post-1" class="elementor-post cityapartments type-cityapartments category-koebenhavn-k">
        <h3><a href="https://cityapartment.dk/da/cityapartments/target-listing/">Gothersgade 1</a></h3>
        <p>Post nr. 1123</p>
        <p>65 m²</p>
        <p>12500 DKK / pr. maaned</p>
      </article>
      <article id="post-2" class="elementor-post cityapartments type-cityapartments category-glostrup-da">
        <h3><a href="https://cityapartment.dk/da/cityapartments/other-listing/">Kildevej 12</a></h3>
        <p>Post nr. 2600</p>
        <p>95 m²</p>
        <p>14250 DKK / pr. maaned</p>
      </article>
      <p>Footer content about the neighborhood.</p>
    </article>
    """

    def test_only_returns_cards_in_target_areas(self):
        apartments = watcher.parse_city_apartment_listings(self.SAMPLE_HTML)

        self.assertEqual(1, len(apartments))
        apt = apartments[0]
        self.assertEqual("Gothersgade 1", apt["name"])
        self.assertEqual("https://cityapartment.dk/da/cityapartments/target-listing/", apt["url"])
        self.assertEqual("12500", apt["price"]["amount"])
        self.assertEqual("City Apartment", apt["source"])

    def test_headers_include_accept_language_to_avoid_waf_block(self):
        # cityapartment.dk returns HTTP 454 for requests missing Accept-Language
        # (confirmed empirically against the live site).
        self.assertIn("Accept-Language", watcher.CITY_APARTMENT_HEADERS)
        self.assertTrue(watcher.CITY_APARTMENT_HEADERS["Accept-Language"])


class FastSlowSourceSplitTests(unittest.TestCase):
    def test_fast_and_slow_source_names_do_not_overlap(self):
        self.assertEqual(set(), watcher.FAST_SOURCE_NAMES & watcher.SLOW_SOURCE_NAMES)

    @patch("watcher.fetch_sweet_homes_apartments", return_value=[{"id": "sh"}])
    @patch("watcher.fetch_propstep_apartments", return_value=[{"id": "ps"}])
    @patch("watcher.fetch_city_apartments", return_value=[{"id": "ca"}])
    @patch("watcher.fetch_cej_apartments", return_value=[{"id": "cej", "status": "available"}])
    def test_fetch_fast_source_apartments_covers_all_fast_sources(
        self, _mock_cej, _mock_city, _mock_propstep, _mock_sweethomes
    ):
        items = watcher.fetch_fast_source_apartments()
        ids = {item["id"] for item in items}
        self.assertEqual({"cej", "ca", "ps", "sh"}, ids)

    @patch("watcher.fetch_cwobel_apartments", return_value=[{"id": "cwobel"}])
    @patch("watcher.fetch_juliliving_apartments", return_value=[{"id": "juli"}])
    @patch("watcher.fetch_capitalbolig_apartments", return_value=[{"id": "capital"}])
    def test_fetch_slow_source_apartments_covers_all_slow_sources(
        self, _mock_capital, _mock_juli, _mock_cwobel
    ):
        items = watcher.fetch_slow_source_apartments()
        ids = {item["id"] for item in items}
        self.assertEqual({"capital", "juli", "cwobel"}, ids)

    def test_slow_source_interval_is_much_slower_than_hot_tier(self):
        self.assertGreater(watcher.SLOW_SOURCE_INTERVAL_SECONDS, watcher.POLL_INTERVALS["HOT"])


if __name__ == "__main__":
    unittest.main()
