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
    @staticmethod
    def _listing(
        name="Lejlighed",
        location="Nørrebrogade 1, 2200 København N",
        amount=12_000,
        **metadata,
    ):
        listing = {
            "name": name,
            "location": {"formatted": location},
            "price": {"amount": amount},
        }
        listing.update(metadata)
        return listing

    def test_accepts_every_agreed_postcode_group(self):
        accepted = [1000, 1499, 1500, 1799, 1800, 2000, 2100, 2150, 2200, 2300, 2400, 2450]
        self.assertTrue(all(is_preferred_postcode(code) for code in accepted))

    def test_rejects_outer_and_explicitly_excluded_postcodes(self):
        rejected = [999, 2001, 2050, 2500, 2605, 2700, 2720, 2770, 2900]
        self.assertTrue(all(not is_preferred_postcode(code) for code in rejected))

    def test_explicit_outer_location_wins_over_year_like_title_number(self):
        listing = self._listing(
            name="Historisk ejendom fra 1800",
            location="Park Allé 10, 2605 Brøndby",
        )
        self.assertFalse(listing_matches_policy(listing))

    def test_explicit_preferred_location_wins_over_title_year(self):
        listing = self._listing(
            name="Nybyggeri 2024",
            location="Nørrebrogade 1, 2200 København N",
        )
        self.assertTrue(listing_matches_policy(listing))

    def test_title_punctuation_does_not_turn_a_year_into_a_postcode(self):
        listing = self._listing(
            name="Nybyggeri, 2024",
            location="Nørrebrogade 1, 2200 København N",
        )
        self.assertTrue(listing_matches_policy(listing))

    def test_conflicting_contextual_name_and_location_postcodes_fail_closed(self):
        listing = self._listing(
            name="Østerbrogade 1, 2100 København Ø",
            location="Nørrebrogade 1, 2200 København N",
        )
        self.assertFalse(listing_matches_policy(listing))

    def test_multiple_location_postcodes_fail_closed(self):
        listing = self._listing(location="Flytter fra 2100 til 2200 København")
        self.assertFalse(listing_matches_policy(listing))

    def test_falls_back_to_contextual_name_postcode_when_location_has_none(self):
        listing = self._listing(
            name="Nørrebrogade 1, 2200 København N",
            location="København N",
        )
        self.assertTrue(listing_matches_policy(listing))

    def test_arbitrary_capitalized_title_number_is_not_postcode_context(self):
        listing = self._listing(
            name="Historisk, 1800 Ejendom",
            location="Brøndby",
        )
        self.assertFalse(listing_matches_policy(listing))

    def test_labeled_name_postcode_is_valid_fallback_context(self):
        for name in ("Lejlighed Post nr. 2200", "Lejlighed postnummer 2200"):
            with self.subTest(name=name):
                listing = self._listing(name=name, location="København N")
                self.assertTrue(listing_matches_policy(listing))

    def test_parses_danish_amounts_and_postcodes(self):
        self.assertEqual(17500, extract_amount("17.500,- kr."))
        self.assertEqual(2799999, extract_amount("2.799.999 kr."))
        self.assertEqual(2400, extract_postcode("Lærkevej 10, 2400 København NV"))

    def test_parses_danish_decimal_amounts_without_appending_oere(self):
        self.assertEqual(17500, extract_amount("17.500,00 kr."))
        self.assertEqual(-17500, extract_amount("-17.500,00 kr."))
        self.assertAlmostEqual(18000.01, extract_amount("18.000,01 kr."))

    def test_danish_decimal_rent_respects_inclusive_boundary(self):
        self.assertTrue(listing_matches_policy(self._listing(amount="18.000,00 kr.")))
        self.assertFalse(listing_matches_policy(self._listing(amount="18.000,01 kr.")))

    def test_numeric_float_amounts_preserve_fractional_value(self):
        self.assertEqual(18000, extract_amount(18000.0))
        self.assertEqual(18000.01, extract_amount(18000.01))

    def test_numeric_and_text_decimal_prices_have_same_boundary_result(self):
        self.assertFalse(listing_matches_policy(self._listing(amount=18000.01)))
        self.assertFalse(listing_matches_policy(self._listing(amount="18.000,01 kr.")))

    def test_nonfinite_numeric_amounts_fail_closed(self):
        for amount in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(amount=amount):
                try:
                    parsed = extract_amount(amount)
                    matches = listing_matches_policy(self._listing(amount=amount))
                except (OverflowError, ValueError) as exc:
                    self.fail(f"non-finite amount raised {type(exc).__name__}")

                self.assertIsNone(parsed)
                self.assertFalse(matches)

    def test_amount_rejects_boolean_and_missing_values(self):
        self.assertIsNone(extract_amount(True))
        self.assertIsNone(extract_amount(None))

    def test_rent_is_inclusive_but_overridden_rent_and_sale_are_strict(self):
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

    def test_valid_numeric_and_string_price_limit_metadata(self):
        try:
            inclusive_results = [
                listing_matches_policy(
                    self._listing(
                        amount=15_000,
                        price_limit=limit,
                        price_limit_inclusive=True,
                    )
                )
                for limit in (15_000, "15000", "15.000")
            ]
        except (TypeError, ValueError) as exc:
            self.fail(f"valid price-limit metadata raised {type(exc).__name__}")

        self.assertEqual([True, True, True], inclusive_results)
        self.assertTrue(
            listing_matches_policy(
                self._listing(
                    amount=14_000,
                    price_limit="15000",
                    price_limit_inclusive=False,
                )
            )
        )

    def test_malformed_price_limit_fails_closed_without_raising(self):
        listing = self._listing(price_limit="not-a-limit")
        try:
            result = listing_matches_policy(listing)
        except (TypeError, ValueError) as exc:
            self.fail(f"malformed price limit raised {type(exc).__name__}")
        self.assertFalse(result)

    def test_non_boolean_price_limit_inclusive_fails_closed(self):
        for inclusive in ("false", "true", 0, None):
            with self.subTest(inclusive=inclusive):
                listing = self._listing(
                    amount=14_000,
                    price_limit=15_000,
                    price_limit_inclusive=inclusive,
                )
                self.assertFalse(listing_matches_policy(listing))

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
        self.assertTrue(
            contains_restricted_eligibility("Kræver medlemskab af en pensionsordning")
        )
        self.assertFalse(contains_restricted_eligibility("Intet medlemskab kræves"))
        self.assertTrue(
            contains_commercial_use("Erhvervslokale indrettet som kontor og butikslokale")
        )
        self.assertFalse(contains_commercial_use("Privat lejlighed med altan"))
        self.assertFalse(
            contains_commercial_use("Bolig tæt på butikker og restauranter")
        )

    def test_negated_membership_requirements_are_not_restricted(self):
        self.assertFalse(contains_restricted_eligibility("Medlemskab kræves ikke"))
        self.assertFalse(contains_restricted_eligibility("Ingen krav om medlemskab"))
        self.assertFalse(
            contains_restricted_eligibility("Der er intet krav om medlemskab")
        )

    def test_cooperative_membership_wording_depends_on_transaction_type(self):
        base = {
            "name": "Nørrebrogade 1",
            "location": {"formatted": "Nørrebrogade 1, 2200 København N"},
            "price": {"amount": 12000},
            "description": "Køber bliver medlem af andelsforeningen",
        }
        self.assertFalse(listing_matches_policy(dict(base, transaction_type="rent")))
        self.assertTrue(
            listing_matches_policy(
                dict(base, transaction_type="cooperative_sale", price={"amount": 2_000_000})
            )
        )

    def test_andelsboligforening_membership_exemption_is_sale_only(self):
        wording = "Køberen skal være medlem af andelsboligforeningen"
        self.assertTrue(
            listing_matches_policy(
                self._listing(
                    amount=2_000_000,
                    transaction_type="cooperative_sale",
                    description=wording,
                )
            )
        )
        self.assertFalse(
            listing_matches_policy(
                self._listing(transaction_type="rent", description=wording)
            )
        )
        self.assertFalse(
            listing_matches_policy(
                self._listing(
                    amount=2_000_000,
                    transaction_type="cooperative_sale",
                    description="Køberen skal være medlem af pensionsordningen",
                )
            )
        )

    def test_explicit_office_for_rent_is_commercial_but_proximity_is_not(self):
        self.assertTrue(contains_commercial_use("Kontor til leje"))
        self.assertFalse(contains_commercial_use("Bolig tæt på butikker og restauranter"))

    def test_textual_negative_price_stays_negative_and_is_rejected(self):
        listing = {
            "name": "Nørrebrogade 1",
            "location": {"formatted": "Nørrebrogade 1, 2200 København N"},
            "price": {"amount": "-1.000 kr."},
        }
        self.assertEqual(-1000, extract_amount("-1.000 kr."))
        self.assertFalse(listing_matches_policy(listing))

    def test_policy_rejects_restricted_and_commercial_listings(self):
        base = {
            "name": "Nørrebrogade 1",
            "location": {"formatted": "Nørrebrogade 1, 2200 København N"},
            "price": {"amount": 12000},
            "transaction_type": "rent",
        }
        self.assertFalse(listing_matches_policy(dict(base, description="Kun for studerende")))
        self.assertFalse(
            listing_matches_policy(dict(base, description="Erhvervslokale indrettet som kontor"))
        )
        cooperative = dict(
            base,
            transaction_type="cooperative_sale",
            price={"amount": 2_000_000},
            description="Køber bliver medlem af andelsforeningen",
        )
        self.assertTrue(listing_matches_policy(cooperative))
        self.assertFalse(
            listing_matches_policy(dict(cooperative, description="Kun for seniorer"))
        )

    def test_canonical_key_keeps_floor_and_transaction_type(self):
        rent_key = canonical_listing_key("Händelsvej 23, 2. th., 2450 København SV", "rent")
        sale_key = canonical_listing_key(
            "Händelsvej 23 2 TH 2450 København SV", "cooperative_sale"
        )
        self.assertNotEqual(rent_key, sale_key)
        self.assertIn("handelsvej 23 2 th 2450 kobenhavn sv", rent_key)


if __name__ == "__main__":
    unittest.main()
