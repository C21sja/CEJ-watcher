import unittest

from housing_sources.readiness import (
    CPH_DOCUMENT_URLS,
    discover_cphomes_post_urls,
    fetch_cphomes,
    fetch_rle,
    parse_cphomes_documents,
    parse_rle_document,
)


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


def cph_page(body, head=""):
    return (
        f"<html><head>{head}</head><body><main><h1>CPH Homes</h1>{body}</main>"
        "<footer>Footer</footer></body></html>"
    )


class CPHHomesSourceTests(unittest.TestCase):
    def test_static_portfolio_returns_nonurgent_readiness_state(self):
        documents = {"https://cphhomes.dk/holmen/": cph_page("<p>Attraktive boliger på Holmen</p>")}
        snapshot = parse_cphomes_documents(documents)
        self.assertEqual([], snapshot.listings)
        self.assertFalse(snapshot.events[0]["urgent"])
        self.assertEqual("CPH Homes monitoring ready", snapshot.events[0]["headline"])

    def test_availability_language_creates_inspection_signal_not_fake_listing(self):
        documents = {
            "https://cphhomes.dk/sydhavnen/": cph_page(
                "<p>Ledig lejlighed, 2450 København SV, husleje 17.500 kr. Kontakt os</p>"
            )
        }
        snapshot = parse_cphomes_documents(documents)
        self.assertEqual([], snapshot.listings)
        self.assertFalse(snapshot.events[0]["urgent"])
        self.assertEqual(
            "CPH Homes availability signal - inspect now", snapshot.events[0]["headline"]
        )
        self.assertEqual("https://cphhomes.dk/sydhavnen/", snapshot.events[0]["url"])

    def test_modified_timestamp_alone_does_not_change_signature(self):
        url = "https://cphhomes.dk/holmen/"
        first = {
            url: cph_page(
                "<p>Samme indhold</p>", '<meta property="article:modified_time" content="2019-01-01">'
            )
        }
        second = {
            url: cph_page(
                "<p>Samme indhold</p>", '<meta property="article:modified_time" content="2026-07-13">'
            )
        }
        self.assertEqual(
            parse_cphomes_documents(first).events[0]["signature"],
            parse_cphomes_documents(second).events[0]["signature"],
        )

    def test_new_same_host_application_link_is_recorded_as_evidence(self):
        url = "https://cphhomes.dk/engholmene/"
        event = parse_cphomes_documents(
            {url: cph_page('<a href="/kontakt/">Skriv dig op</a>')}
        ).events[0]
        self.assertIn("application-link:https://cphhomes.dk/kontakt/", event["signals"])

    def test_unknown_page_is_ignored_and_new_external_action_is_review_evidence(self):
        unknown = {"https://cphhomes.dk/valby/": cph_page("<p>Ledig bolig, 2500 Valby</p>")}
        self.assertEqual([], parse_cphomes_documents(unknown).events)
        url = "https://cphhomes.dk/engholmene/"
        event = parse_cphomes_documents(
            {url: cph_page('<a href="https://apply.example/bolig">Skriv dig op</a>')}
        ).events[0]
        self.assertEqual(url, event["url"])
        self.assertIn("external-application-review:apply.example", event["signals"])

    def test_discovers_only_same_host_https_article_links(self):
        home = cph_page(
            '<article><a href="/nyheder/ledig-paa-holmen/">Ny bolig</a></article>'
            '<article><a href="https://evil.example/post">Falsk</a></article>'
        )
        self.assertEqual(
            ["https://cphhomes.dk/nyheder/ledig-paa-holmen/"],
            discover_cphomes_post_urls(home),
        )

    def test_fetch_uses_only_pinned_https_canonical_pages(self):
        calls = []

        def fetch_text(url):
            calls.append(url)
            return cph_page("<p>Relevant portfolio</p>")

        snapshot = fetch_cphomes(fetch_text)
        self.assertEqual(set(CPH_DOCUMENT_URLS.values()), set(calls))
        self.assertEqual(len(CPH_DOCUMENT_URLS), len(snapshot.events))
        self.assertTrue(all(event["url"].startswith("https://cphhomes.dk/") for event in snapshot.events))

    def test_fetch_follows_newly_published_same_host_post_once(self):
        post_url = "https://cphhomes.dk/nyheder/ledig-paa-holmen/"
        calls = []

        def fetch_text(url):
            calls.append(url)
            if url == CPH_DOCUMENT_URLS["home"]:
                return cph_page(f'<article><a href="{post_url}">Ny bolig</a></article>')
            if url == post_url:
                return cph_page("<p>Ledig lejlighed på Holmen</p>")
            return cph_page("<p>Relevant portfolio</p>")

        snapshot = fetch_cphomes(fetch_text)
        self.assertEqual(1, calls.count(post_url))
        self.assertTrue(
            any(event["id"].startswith("readiness:cphhomes:post:") for event in snapshot.events)
        )


if __name__ == "__main__":
    unittest.main()
