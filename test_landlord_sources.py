import unittest

from housing_sources import SourceContractError
from housing_sources.landlords import (
    TAURUS_URL,
    fetch_lejeboligmaegleren,
    fetch_norhjem,
    fetch_taurus,
    parse_norhjem_results,
    parse_taurus_detail,
    parse_taurus_overview,
)


TAURUS_HTML = """
<div class="rental-item" data-price="1" data-rooms="99" data-living-area="999">
  This non-anchor decoy must not become a candidate.
</div>
<a class="teaser rental-item featured" href="/boligudlejning/lejebolig?id=101"
   data-cities="koebenhavn-n" data-rooms="2-vaer"
   data-living-area="76" data-price="17500">
  <h3>Centralt paa Noerrebro</h3>
</a>
<a class="rental-item" href="/boligudlejning/lejebolig?campaign=sommer&amp;id=102"
   data-cities="koebenhavn-s" data-rooms="4-vaer"
   data-living-area="115" data-price="20500">
  <h3>Over prisgraensen</h3>
</a>
<a class="rental-item" href="/boligudlejning/lejebolig?id=103"
   data-cities="valby" data-rooms="2-vaer"
   data-living-area="60" data-price="12000">
  <h3>Bolig i Valby</h3>
</a>
<a class="rental-item" href="/boligudlejning/lejebolig?id=104"
   data-cities="koebenhavn-s" data-rooms="3-vaer"
   data-living-area="81" data-price="15000">
  <h3>Reserveret paa Amager</h3>
</a>
<a class="rental-item" href="/boligudlejning/lejebolig?id=105"
   data-cities="koebenhavn-n" data-rooms="1-vaer"
   data-living-area="35" data-price="8000">
  <h3>Studiebolig</h3>
</a>
<section data-price="999" data-rooms="88" data-living-area="888">
  Attributes outside rental anchors must not leak into a card.
</section>
"""


def taurus_detail(
    *,
    status="ledig",
    rent="17.500 kr.",
    street="Noerrebrogade",
    house_number="190, 5. th.",
    postcode="2200",
    city="Koebenhavn N",
    study=None,
    audience=None,
    requirements=None,
    omit=(),
):
    values = {
        "Status": status,
        "Husleje": rent,
        "Vejnavn": street,
        "Husnummer": house_number,
        "Postnummer": postcode,
        "By": city,
        "Studiebolig": study,
        "Maalgruppe": audience,
        "Krav": requirements,
    }
    labels = []
    for label, value in values.items():
        if label in omit or value is None:
            continue
        rendered_label = "Maalgruppe" if label == "Maalgruppe" else label
        labels.append(f"<p><strong>{rendered_label}:</strong> {value}</p>")
    return "<main>" + "".join(labels) + "</main>"


TAURUS_DETAILS = {
    "101": taurus_detail(),
    "103": taurus_detail(
        rent="12.000 kr.",
        street="Valby Langgade",
        house_number="1",
        postcode="2500",
        city="Valby",
        study="Nej",
    ),
    "104": taurus_detail(
        status="reserveret",
        rent="15.000 kr.",
        street="Amagerbrogade",
        house_number="4",
        postcode="2300",
        city="Koebenhavn S",
        study="Nej",
    ),
    "105": taurus_detail(
        rent="8.000 kr.",
        street="Noerrebrogade",
        house_number="5",
        postcode="2200",
        city="Koebenhavn N",
        study="Ja",
    ),
}


class TaurusSourceTests(unittest.TestCase):
    @staticmethod
    def _candidate(record_id="101", price=17_500, rooms=2, size_sqm=76):
        return {
            "record_id": record_id,
            "url": (
                "https://www.taurus.dk/boligudlejning/lejebolig"
                f"?id={record_id}"
            ),
            "price": price,
            "rooms": rooms,
            "size_sqm": size_sqm,
        }

    def test_overview_is_bounded_to_rental_anchors_and_reads_card_attributes(self):
        candidates = parse_taurus_overview(TAURUS_HTML)

        self.assertEqual(
            ["101", "102", "103", "104", "105"],
            [candidate["record_id"] for candidate in candidates],
        )
        self.assertEqual(
            {"price": 17_500, "rooms": 2, "size_sqm": 76},
            {
                field: candidates[0][field]
                for field in ("price", "rooms", "size_sqm")
            },
        )
        self.assertEqual(
            "https://www.taurus.dk/boligudlejning/lejebolig?id=101",
            candidates[0]["url"],
        )

    def test_overview_requires_cards_or_a_verified_empty_state(self):
        with self.assertRaises(SourceContractError):
            parse_taurus_overview("<main><p>Udlejningsejendomme</p></main>")

        self.assertEqual(
            [],
            parse_taurus_overview(
                "<main><p>Der er ingen ledige lejemaal i oejeblikket.</p></main>"
            ),
        )

    def test_detail_requires_every_required_label_inside_main(self):
        # "By" (city) is deliberately not required: a live capture found
        # Taurus's sidebar never includes it, only Postnummer.
        required = ("Status", "Husleje", "Vejnavn", "Husnummer", "Postnummer")
        for label in required:
            with self.subTest(label=label):
                with self.assertRaises(SourceContractError):
                    parse_taurus_detail(
                        self._candidate(),
                        taurus_detail(omit={label}),
                    )

        outside_main = taurus_detail().replace("<main>", "<div>").replace(
            "</main>", "</div><main><p>Boligdetaljer</p></main>"
        )
        with self.assertRaises(SourceContractError):
            parse_taurus_detail(self._candidate(), outside_main)

    def test_detail_statuses_support_available_and_reserved_state_seeding(self):
        expected = {
            "ledig": "Available",
            "reserveret": "Reserved",
            "udlejet": "Reserved",
            "under kontrakt": "Reserved",
        }
        for source_status, normalized_status in expected.items():
            with self.subTest(status=source_status):
                listing = parse_taurus_detail(
                    self._candidate(),
                    taurus_detail(status=source_status),
                )
                self.assertEqual(normalized_status, listing["status"])

        self.assertIsNone(
            parse_taurus_detail(
                self._candidate(),
                taurus_detail(status="afventer vurdering"),
            )
        )

    def test_detail_uses_current_rent_and_reapplies_the_shared_price_policy(self):
        at_limit = parse_taurus_detail(
            self._candidate(price=9_000),
            taurus_detail(rent="18.000 kr."),
        )
        self.assertEqual(18_000, at_limit["price"]["amount"])

        self.assertIsNone(
            parse_taurus_detail(
                self._candidate(price=17_500),
                taurus_detail(rent="18.000,01 kr."),
            )
        )

    def test_detail_uses_all_restriction_labels_and_fails_closed_on_area(self):
        restrictions = (
            {"study": "Ja"},
            {"audience": "Kun for seniorer"},
            {"requirements": "Kun for medlemmer"},
        )
        for values in restrictions:
            with self.subTest(values=values):
                self.assertIsNone(
                    parse_taurus_detail(
                        self._candidate(),
                        taurus_detail(**values),
                    )
                )

        allowed = parse_taurus_detail(
            self._candidate(),
            taurus_detail(
                study="Nej",
                audience="Alle",
                requirements="Intet medlemskab kraeves",
            ),
        )
        self.assertIn("Alle", allowed["raw_text"])
        self.assertIn("Intet medlemskab kraeves", allowed["raw_text"])
        self.assertIsNone(
            parse_taurus_detail(
                self._candidate(),
                taurus_detail(postcode="ukendt"),
            )
        )

    def test_fetch_uses_official_overview_shortlists_then_returns_exact_results(self):
        calls = []

        def fetch_text(url):
            calls.append(url)
            if url == TAURUS_URL:
                return TAURUS_HTML
            return TAURUS_DETAILS[url.split("id=")[-1]]

        snapshot = fetch_taurus(fetch_text)

        self.assertEqual(TAURUS_URL, calls[0])
        self.assertFalse(any("id=102" in url for url in calls))
        self.assertEqual("Taurus", snapshot.source)
        self.assertEqual(["taurus:101", "taurus:104"], [item["id"] for item in snapshot.listings])
        self.assertEqual(["Available", "Reserved"], [item["status"] for item in snapshot.listings])
        self.assertEqual([2, 3], [item["rooms"] for item in snapshot.listings])
        self.assertEqual([76, 81], [item["size_sqm"] for item in snapshot.listings])
        self.assertTrue(all(item["source"] == "Taurus" for item in snapshot.listings))
        self.assertEqual(
            [
                "https://www.taurus.dk/boligudlejning/lejebolig?id=101",
                "https://www.taurus.dk/boligudlejning/lejebolig?id=104",
            ],
            [item["url"] for item in snapshot.listings],
        )

    def test_fetch_does_not_request_zero_negative_or_over_cap_candidates(self):
        overview = """
        <a class="rental-item" href="?id=zero" data-price="0" data-rooms="1" data-living-area="30"></a>
        <a class="rental-item" href="?id=negative" data-price="-1" data-rooms="1" data-living-area="30"></a>
        <a class="rental-item" href="?id=limit" data-price="18000" data-rooms="2" data-living-area="60"></a>
        <a class="rental-item" href="?id=over" data-price="18001" data-rooms="2" data-living-area="60"></a>
        """
        calls = []

        def fetch_text(url):
            calls.append(url)
            return overview if url == TAURUS_URL else taurus_detail(rent="18.000 kr.")

        snapshot = fetch_taurus(fetch_text)

        self.assertEqual(["taurus:limit"], [item["id"] for item in snapshot.listings])
        self.assertTrue(any("id=limit" in url for url in calls))
        self.assertFalse(any("id=zero" in url for url in calls))
        self.assertFalse(any("id=negative" in url for url in calls))
        self.assertFalse(any("id=over" in url for url in calls))

    def test_fetch_raises_when_every_shortlisted_detail_breaks_the_contract(self):
        overview = """
        <a class="rental-item" href="?id=201" data-price="12000" data-rooms="2" data-living-area="60"></a>
        <a class="rental-item" href="?id=202" data-price="13000" data-rooms="3" data-living-area="70"></a>
        """

        def fetch_text(url):
            return overview if url == TAURUS_URL else "<main><p>Detaljer mangler</p></main>"

        with self.assertRaises(SourceContractError):
            fetch_taurus(fetch_text)

    def test_fetch_isolates_one_bad_detail_when_another_detail_is_valid(self):
        overview = """
        <a class="rental-item" href="?id=301" data-price="12000" data-rooms="2" data-living-area="60"></a>
        <a class="rental-item" href="?id=302" data-price="13000" data-rooms="3" data-living-area="70"></a>
        """

        def fetch_text(url):
            if url == TAURUS_URL:
                return overview
            if url.endswith("id=301"):
                return "<main><p>Detaljer mangler</p></main>"
            return taurus_detail(rent="13.000 kr.")

        snapshot = fetch_taurus(fetch_text)

        self.assertEqual(["taurus:302"], [item["id"] for item in snapshot.listings])


class LejeboligmaeglerenSourceTests(unittest.TestCase):
    def test_paginates_until_empty_and_maps_actionable_states(self):
        calls = []
        pages_payloads = []
        pages = {
            1: {
                "Cases": [
                    {
                        "Id": 701,
                        "Address": "Sluseholmen 1",
                        "City": {"ZipCode": 2450, "Name": "København SV"},
                        "Rent": 9200,
                        "State": "Ledig",
                        "Rooms": 2,
                        "Size": 61,
                        "AcquisitionDate": "2026-08-01",
                        "Description": "Familiebolig",
                        "Tags": [],
                        "UnitType": "Lejlighed",
                    },
                    {
                        "Id": 702,
                        "Address": "Amagerbrogade 2",
                        "City": {"ZipCode": 2300, "Name": "København S"},
                        "Rent": 18000,
                        "State": "Under opsigelse",
                        "Rooms": 3,
                        "Size": 78,
                        "AcquisitionDate": "2026-09-01",
                        "Description": "",
                        "Tags": [],
                    },
                    {
                        "Id": 703,
                        "Address": "Amagerbrogade 4",
                        "City": {"ZipCode": 2300, "Name": "København S"},
                        "Rent": 15000,
                        "State": "Kontrakt under udarbejdelse",
                        "Rooms": 2,
                        "Size": 65,
                        "AcquisitionDate": "2026-10-01",
                        "Description": "",
                        "Tags": [],
                    },
                    {
                        "Id": 704,
                        "Address": "Amagerbrogade 6",
                        "City": {"ZipCode": 2300, "Name": "København S"},
                        "Rent": 8000,
                        "State": "Ledig",
                        "Rooms": 1,
                        "Size": 30,
                        "Description": "",
                        "Tags": [99],
                        "UnitType": 9,
                    },
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
            if url.endswith("UnitCaseTypes"):
                return [{"Id": 9, "Name": "Studiebolig"}]
            return [{"Id": 99, "Name": "Kun for studerende"}]

        snapshot = fetch_lejeboligmaegleren(post_json, fetch_json, page_size=4)

        self.assertEqual([1, 2], calls)
        self.assertEqual(
            ["Available", "Available", "Reserved"],
            [item["status"] for item in snapshot.listings],
        )
        self.assertEqual(
            "https://lejeboligmaegleren.dk/cases/701/", snapshot.listings[0]["url"]
        )
        self.assertEqual(61, snapshot.listings[0]["size_sqm"])
        self.assertEqual("2026-08-01", snapshot.listings[0]["availableFrom"])
        required_payload_keys = {
            "PageIndex",
            "PageSize",
            "MaxRent",
            "ZipCodes",
            "TypeIds",
            "TagIds",
            "MinRooms",
            "MaxRooms",
            "MinSize",
            "MaxSize",
            "MinFloor",
            "MaxFloor",
            "AcquisitionDateFrom",
            "AcquisitionDateTo",
            "OnlyAvailable",
            "RentalPeriod",
            "FacilityIds",
            "AddressQuery",
        }
        self.assertEqual(required_payload_keys, set(pages_payloads[0]))

    def test_rejects_studiebolig_unit_type_and_restricted_tag(self):
        pages = {1: {"Cases": [], "HasMorePages": False}}

        def post_json(_url, payload):
            return pages[payload["PageIndex"]]

        def fetch_json(url):
            if url.endswith("UnitCaseTypes"):
                return [{"Id": 9, "Name": "Studiebolig"}]
            return [{"Id": 99, "Name": "Kun for studerende"}]

        pages[1]["Cases"] = [
            {
                "Id": 801,
                "Address": "Studievej 1",
                "City": {"ZipCode": 2200, "Name": "København N"},
                "Rent": 8000,
                "State": "Ledig",
                "Rooms": 1,
                "Size": 25,
                "Tags": [99],
                "UnitType": 9,
            }
        ]
        snapshot = fetch_lejeboligmaegleren(post_json, fetch_json)
        self.assertEqual([], snapshot.listings)

    def test_missing_cases_key_is_not_treated_as_empty(self):
        with self.assertRaisesRegex(Exception, "Cases key"):
            fetch_lejeboligmaegleren(
                lambda _url, _payload: {"HasMorePages": False},
                lambda _url: [],
            )

    def test_city_not_an_object_fails_closed(self):
        pages = {1: {"Cases": [{"Id": 1, "Address": "X", "City": "not-an-object", "Rent": 8000, "State": "Ledig"}]}}

        def post_json(_url, payload):
            return pages[payload["PageIndex"]]

        with self.assertRaisesRegex(Exception, "City is not an object"):
            fetch_lejeboligmaegleren(post_json, lambda _url: [])


NORHJEM_RESULTS = [
    {
        "address": "Willemoesgade 1, 2. tv.",
        "zipCode": 2100,
        "city": "København Ø",
        "url": "/ejendomme/osterbro/willemoesgade-1-2-tv/",
        "price": 9150,
        "rooms": 2,
        "area": 55,
        "status": "Ledig",
        "moveInDate": "2026-08-01",
        "type": "Lejlighed",
    },
    {
        "address": "Amagerbrogade 10",
        "zipCode": 2300,
        "city": "København S",
        "url": "/ejendomme/amager/student-1/",
        "price": 5523,
        "rooms": 1,
        "area": 30,
        "status": "Ledig",
        "moveInDate": "2026-09-01",
        "type": "Lejlighed",
    },
    {
        "address": "Roskildevej 33",
        "zipCode": 2000,
        "city": "Frederiksberg",
        "url": "/ejendomme/frederiksberg/reserved/",
        "price": 12500,
        "rooms": 2,
        "area": 65,
        "status": "Reserveret",
        "moveInDate": "2026-10-01",
        "type": "Lejlighed",
    },
]


class NorhjemSourceTests(unittest.TestCase):
    def test_normalizes_live_api_results_and_retains_reserved_state(self):
        listings = parse_norhjem_results(
            NORHJEM_RESULTS, blocked_urls={"/ejendomme/amager/student-1/"}
        )
        self.assertEqual(
            [
                "norhjem:/ejendomme/osterbro/willemoesgade-1-2-tv/",
                "norhjem:/ejendomme/frederiksberg/reserved/",
            ],
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
            [
                {"maxPrice": "18000", "sort": ""},
                {"maxPrice": "18000", "sort": "", "facilities": "Kun for studerende"},
            ],
            form_calls,
        )
        self.assertEqual(2, len(snapshot.listings))
        self.assertTrue(all(url.startswith("https://norhjem.dk/ejendomme/") for url in detail_calls))

    def test_rejects_restricted_detail_text_even_when_not_in_student_search(self):
        def post_form(_url, payload):
            if payload.get("facilities") == "Kun for studerende":
                return []
            return [NORHJEM_RESULTS[0]]

        def fetch_text(_url):
            return "<main>Denne bolig kraever medlemskab af en pensionsordning</main>"

        snapshot = fetch_norhjem(post_form, fetch_text)
        self.assertEqual([], snapshot.listings)

    def test_wrong_api_shape_is_not_a_valid_empty_feed(self):
        with self.assertRaisesRegex(Exception, "JSON list"):
            fetch_norhjem(lambda _url, _payload: {"results": []}, lambda _url: "")

    def test_raises_when_every_candidate_detail_fetch_fails(self):
        def post_form(_url, payload):
            if payload.get("facilities") == "Kun for studerende":
                return []
            return [NORHJEM_RESULTS[0]]

        def fetch_text(_url):
            return "   "

        with self.assertRaisesRegex(Exception, "restriction-screening contract"):
            fetch_norhjem(post_form, fetch_text)


if __name__ == "__main__":
    unittest.main()
