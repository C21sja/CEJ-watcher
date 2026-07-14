import json
import unittest
from pathlib import Path
from unittest.mock import patch

from housing_sources.brikk import fetch_brikk, parse_brikk_page
from housing_sources.kobenhavn_dk import (
    ORIGIN_VERIFIERS,
    _akutbolig_inventory_url,
    _akutbolig_record,
    fetch_kobenhavn,
    parse_candidates,
    verify_candidate,
)


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
        self.assertEqual(
            ["Händelsvej 23, 2. th., 2450 København SV"], [item["name"] for item in listings]
        )

    def test_fetches_each_page_once_and_deduplicates_links(self):
        calls = []

        def fetch_text(url):
            calls.append(url)
            if "/ejendom/" in url:
                return (
                    '<main><section class="property-status"><h1>Händelsvej 23, 2. th.</h1>'
                    "<p>Aktiv andelsbolig</p></section></main>"
                )
            return BRIKK_PAGE if "p_page=2" not in url else BRIKK_PAGE.split('<a class="page-numbers"')[0]

        snapshot = fetch_brikk(fetch_text, max_pages=5)
        search_calls = [url for url in calls if "/boliger-til-salg/" in url]
        self.assertEqual(2, len(search_calls))
        self.assertTrue(all("type%5B%5D=Andelsbolig" in url for url in search_calls))
        self.assertEqual(1, len(snapshot.listings))

    def test_detail_with_accepted_offer_is_rejected(self):
        snapshot = fetch_brikk(
            lambda url: (
                BRIKK_PAGE.split('<a class="page-numbers"')[0]
                if "/boliger-til-salg/" in url
                else '<main><section class="property-status">Købstilbud allerede accepteret</section></main>'
            )
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

        self.assertEqual(
            ["brikk:handelsvej-23-2-th"], [item["id"] for item in fetch_brikk(fetch_text).listings]
        )

    def test_unrecognized_detail_status_container_fails_closed(self):
        with self.assertRaisesRegex(Exception, "primary status container"):
            fetch_brikk(
                lambda url: (
                    BRIKK_PAGE.split('<a class="page-numbers"')[0]
                    if "/boliger-til-salg/" in url
                    else "<main><p>Aktiv bolig</p><aside>Andre boliger er solgt</aside></main>"
                )
            )


# Row markup below mirrors a sanitized live capture (12 July 2026) of
# https://www.kobenhavn.dk/bolig: plain `<div class="row coloN">` cards,
# not an HTML table. Rental rows carry the origin link (akutbolig.dk) on
# the address cell and already include the postcode/city in that cell's
# text. Cooperative-sale rows link internally to kobenhavn.dk's own photo
# popup from the address cell; the real broker link is in the final
# "Mægler" cell, and the postcode/city comes only from the section
# heading, not the row itself.
KOBENHAVN_PAGE = """
<h4><strong>Lejligheder til leje, K\u00f8benhavn og omegn.</strong></h4>
<div class="row coloh"><div class="col-xs-6 nopadding">Kort Adresse</div><div class="col-xs-2 text-right nopadding">Leje</div><div class="col-xs-1 text-right nopadding">Rum</div><div class="col-xs-1 text-right nopadding">m2</div><div class="col-xs-2 text-right nopadding">m2 pris</div></div>
<div class="row colo0"><div class="col-xs-6 nopadding"><a href="https://www.akutbolig.dk/vis/486255#utm_source=dkby.net" target="_blank" rel="nofollow"><img src="//www.kobenhavn.dk/im/map.png" border="0">H.C. Andersens Boulevard, 1553 K\u00f8benhavn V</a></div>
<div class="col-xs-2 text-right nopadding">4500</div>
<div class="col-xs-1 text-right nopadding">1</div>
<div class="col-xs-1 text-right nopadding">20</div>
<div class="col-xs-2 text-right nopadding">225</div>
</div>
<h4><strong>2450 K\u00f8benhavn SV - Andelsbolig</strong></h4>
<div class="row coloh"><div class="col-xs-3 nopadding">Se Adresse</div><div class="col-xs-2 text-right nopadding">Pris</div><div class="col-xs-1 text-right nopadding">Rum</div><div class="col-xs-1 text-right nopadding">m2</div><div class="col-xs-2 text-right nopadding">m2 pris</div><div class="col-xs-3 nopadding">M\u00e6gler</div></div>
<div class="topoff" id="1723216"></div><div class="row colo1 text-success" onclick="toggleDiv('1723216')">
<div class="col-xs-3 nopadding"><a href="//www.kobenhavn.dk/bolig#1723216" class="screenshot"><img src="//www.kobenhavn.dk/im/camera.png" border="0"></a>H\u00e4ndelsvej 23, 2 th</div>
<div class="col-xs-2 text-right nopadding">1.848.000</div>
<div class="col-xs-1 text-right nopadding">2</div>
<div class="col-xs-1 text-right nopadding">61</div>
<div class="col-xs-2 text-right nopadding">30.295</div>
<div class="col-xs-3 nopadding overhide"><a href="https://broker.example/handelsvej-23" target="_blank" rel="nofollow">BROKER</a></div>
</div>
"""


class KobenhavnSourceTests(unittest.TestCase):
    def test_parses_rental_and_cooperative_candidates_with_strict_limits(self):
        candidates = parse_candidates(KOBENHAVN_PAGE)
        self.assertEqual(
            {"rent", "cooperative_sale"}, {item["transaction_type"] for item in candidates}
        )
        rent = next(item for item in candidates if item["transaction_type"] == "rent")
        self.assertEqual(15000, rent["price_limit"])
        self.assertFalse(rent["price_limit_inclusive"])

    def test_cooperative_row_uses_broker_link_and_heading_location(self):
        sale = next(
            item for item in parse_candidates(KOBENHAVN_PAGE) if item["transaction_type"] == "cooperative_sale"
        )
        self.assertEqual("broker.example", sale["origin_host"])
        self.assertIn("2450 København SV", sale["address"])
        self.assertEqual(2450, sale["postcode"])

    def test_rejects_stale_detail_and_accepts_current_inventory_membership(self):
        # akutbolig.dk itself is not currently registered in ORIGIN_VERIFIERS
        # (a live capture found it has become a client-rendered app with no
        # server-rendered category-page markup), but `_akutbolig_record`
        # correctly implements the general "membership in a rendered
        # inventory page" pattern that a future host could reuse, so it is
        # exercised here with the registry patched in for this one test.
        candidate = parse_candidates(KOBENHAVN_PAGE)[0]
        with patch.dict(ORIGIN_VERIFIERS, {"akutbolig.dk": _akutbolig_record}):
            stale = verify_candidate(candidate, lambda _url: "Denne bolig er ikke længere aktiv")
            self.assertIsNone(stale)

            def active_fetch(url):
                if url.endswith("/koebenhavn-v"):
                    return (
                        '<a href="/vis/486255">H.C. Andersens Boulevard 10, 2. th.</a>'
                        "<span>4.750 kr.</span><span>1 værelse</span><span>20 m²</span>"
                    )
                return ""

            active = verify_candidate(candidate, active_fetch)
        self.assertEqual("kobenhavn:rent:akutbolig.dk:486255", active["id"])
        self.assertEqual(4750, active["price"]["amount"])

    def test_unsupported_origin_is_not_fetched(self):
        candidate = next(
            item for item in parse_candidates(KOBENHAVN_PAGE) if item["transaction_type"] == "cooperative_sale"
        )
        self.assertIsNone(verify_candidate(candidate, lambda _url: "Til salg"))

    def test_captured_origin_manifest_verifiers_are_a_subset_of_the_real_scan(self):
        manifest_path = Path("tests/fixtures/kobenhavn_dk/origin_manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(manifest["captured_at"])
        self.assertEqual(set(manifest["verified_hosts"]), set(ORIGIN_VERIFIERS))
        self.assertTrue(set(ORIGIN_VERIFIERS) <= set(manifest["origin_hosts"]))
        self.assertTrue(set(manifest["pending_manual_review_hosts"]) <= set(manifest["origin_hosts"]))
        self.assertEqual(
            set(manifest["origin_hosts"]),
            set(manifest["verified_hosts"]) | set(manifest["pending_manual_review_hosts"]),
        )

    def test_private_or_non_https_origin_is_never_fetched(self):
        candidate = dict(
            next(item for item in parse_candidates(KOBENHAVN_PAGE) if item["transaction_type"] == "cooperative_sale"),
            origin_url="http://127.0.0.1/listing",
            origin_host="127.0.0.1",
        )
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
                return (
                    '<a href="/vis/486255">H.C. Andersens Boulevard 10, 2. th.</a>'
                    "<span>4.500 kr.</span><span>1 værelse</span><span>20 m²</span>"
                )
            return ""

        with patch.dict(ORIGIN_VERIFIERS, {"akutbolig.dk": _akutbolig_record}):
            snapshot = fetch_kobenhavn(fetch_text)
        self.assertEqual(
            ["kobenhavn:rent:akutbolig.dk:486255"], [item["id"] for item in snapshot.listings]
        )
        self.assertEqual("manual_review", snapshot.diagnostics[0]["outcome"])
        self.assertIn("broker.example", snapshot.diagnostics[0]["origin_url"])
        self.assertEqual("inspection", snapshot.events[0]["kind"])
        self.assertFalse(snapshot.events[0]["urgent"])

    def test_fetch_treats_every_current_candidate_as_manual_review_by_default(self):
        def fetch_text(url):
            if url == "https://www.kobenhavn.dk/bolig":
                return KOBENHAVN_PAGE
            return ""

        snapshot = fetch_kobenhavn(fetch_text)
        self.assertEqual([], snapshot.listings)
        self.assertEqual(2, len(snapshot.diagnostics))
        self.assertTrue(all(item["outcome"] == "manual_review" for item in snapshot.diagnostics))


if __name__ == "__main__":
    unittest.main()
