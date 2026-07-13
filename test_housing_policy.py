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
