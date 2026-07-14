import unittest

from housing_sources.brikk import fetch_brikk, parse_brikk_page


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


if __name__ == "__main__":
    unittest.main()
