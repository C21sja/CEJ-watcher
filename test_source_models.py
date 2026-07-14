import unittest

import housing_policy
import watcher


class SourceModelTests(unittest.TestCase):
    def test_source_snapshots_have_independent_collection_defaults(self):
        from housing_sources import SourceSnapshot

        first = SourceSnapshot(source="First")
        second = SourceSnapshot(source="Second")

        first.listings.append({"id": "first-listing"})
        first.events.append({"type": "first-event"})
        first.diagnostics.append("first-diagnostic")

        self.assertEqual([], second.listings)
        self.assertEqual([], second.events)
        self.assertEqual([], second.diagnostics)

    def test_source_spec_records_cadence_and_defaults_to_baseline(self):
        from housing_sources import SourceSnapshot, SourceSpec

        fetch = lambda: SourceSnapshot("Findbolig")
        spec = SourceSpec(name="Findbolig", cadence="fast", fetch=fetch)

        self.assertEqual("Findbolig", spec.name)
        self.assertEqual("fast", spec.cadence)
        self.assertIs(fetch, spec.fetch)
        self.assertTrue(spec.baseline)


class ListingDeduplicationTests(unittest.TestCase):
    def test_prefers_lower_priority_while_preserving_first_seen_key_order(self):
        listings = [
            {
                "id": "aggregator-main",
                "canonical_key": "rent:main-street",
                "source": "Aggregator",
                "source_priority": 40,
            },
            {
                "id": "second",
                "canonical_key": "rent:second-street",
                "source": "Second",
                "source_priority": 20,
            },
            {
                "id": "origin-main",
                "canonical_key": "rent:main-street",
                "source": "Origin",
                "source_priority": 10,
            },
            {"id": "fallback", "source": "Fallback high", "source_priority": 50},
            {"id": "fallback", "source": "Fallback low", "source_priority": 5},
            {
                "id": "last",
                "canonical_key": "rent:last-street",
                "source": "Last",
            },
        ]

        result = housing_policy.deduplicate_listings(listings)

        self.assertEqual(
            ["Origin", "Second", "Fallback low", "Last"],
            [listing["source"] for listing in result],
        )

    def test_numeric_string_priority_is_compared_as_an_integer(self):
        listings = [
            {
                "id": "aggregator",
                "canonical_key": "rent:same-street",
                "source": "Aggregator",
                "source_priority": 40,
            },
            {
                "id": "origin",
                "canonical_key": "rent:same-street",
                "source": "Origin",
                "source_priority": "10",
            },
        ]

        result = housing_policy.deduplicate_listings(listings)

        self.assertEqual(["Origin"], [listing["source"] for listing in result])

    def test_default_priority_can_be_replaced_but_equal_priority_keeps_first(self):
        listings = [
            {
                "id": "default-first",
                "canonical_key": "rent:default-priority",
                "source": "Default first",
            },
            {
                "id": "lower-priority",
                "canonical_key": "rent:default-priority",
                "source": "Lower priority",
                "source_priority": 40,
            },
            {
                "id": "tie-first",
                "canonical_key": "rent:equal-priority",
                "source": "Tie first",
                "source_priority": 10,
            },
            {
                "id": "tie-second",
                "canonical_key": "rent:equal-priority",
                "source": "Tie second",
                "source_priority": 10,
            },
        ]

        result = housing_policy.deduplicate_listings(listings)

        self.assertEqual(
            ["Lower priority", "Tie first"],
            [listing["source"] for listing in result],
        )

    def test_malformed_priorities_fall_back_to_default_priority(self):
        listings = [
            {
                "id": "valid-first",
                "canonical_key": "rent:invalid-new",
                "source": "Valid first",
                "source_priority": 40,
            },
            {
                "id": "invalid-new",
                "canonical_key": "rent:invalid-new",
                "source": "Invalid new",
                "source_priority": "urgent",
            },
            {
                "id": "invalid-first",
                "canonical_key": "rent:invalid-existing",
                "source": "Invalid first",
                "source_priority": None,
            },
            {
                "id": "valid-replacement",
                "canonical_key": "rent:invalid-existing",
                "source": "Valid replacement",
                "source_priority": 40,
            },
        ]

        result = housing_policy.deduplicate_listings(listings)

        self.assertEqual(
            ["Valid first", "Valid replacement"],
            [listing["source"] for listing in result],
        )


class PriceFormattingTests(unittest.TestCase):
    def test_formats_total_and_monthly_prices_with_danish_thousands(self):
        self.assertEqual(
            "2.795.000 kr.",
            watcher.format_price_for_display(2_795_000, "total"),
        )
        self.assertEqual(
            "17.500 kr/month",
            watcher.format_price_for_display(17_500, "month"),
        )

    def test_unknown_and_blank_prices_keep_existing_behavior(self):
        self.assertEqual("Unknown", watcher.format_price_for_display(" Unknown "))
        self.assertEqual("Unknown", watcher.format_price_for_display("  "))

    def test_listing_fields_pass_price_period_and_default_to_month(self):
        sale_fields = watcher.build_listing_fields(
            {
                "status": "Available",
                "price": {"amount": 2_795_000},
                "price_period": "total",
                "location": {"formatted": "Sale Street 1, 2100 Copenhagen"},
            }
        )
        monthly_fields = watcher.build_listing_fields(
            {
                "status": "Available",
                "price": {"amount": 17_500},
                "location": {"formatted": "Rental Street 1, 2100 Copenhagen"},
            }
        )

        sale_price = next(field["value"] for field in sale_fields if field["name"] == "Price")
        monthly_price = next(
            field["value"] for field in monthly_fields if field["name"] == "Price"
        )
        self.assertEqual("2.795.000 kr.", sale_price)
        self.assertEqual("17.500 kr/month", monthly_price)


if __name__ == "__main__":
    unittest.main()
