import unittest
from unittest.mock import patch

import watcher
from housing_sources import SourceSnapshot, SourceSpec


def rental(listing_id, address, price, source, status="Available", canonical_key=None):
    return {
        "id": listing_id,
        "status": status,
        "name": address,
        "price": {"amount": price},
        "location": {"formatted": address},
        "availableFrom": "See link for info",
        "url": f"https://example.test/{listing_id}",
        "source": source,
        "transaction_type": "rent",
        "price_period": "month",
        "canonical_key": canonical_key,
        "source_priority": 20,
    }


def readiness(event_id, source, signature, closed=False, urgent=False, signals=(), kind="project_update"):
    return {
        "id": event_id,
        "source": source,
        "headline": f"{source} current status",
        "description": "Inspect the official page for details.",
        "signature": signature,
        "url": "https://example.test/status",
        "urgent": urgent,
        "registration_closed": closed,
        "signals": list(signals),
        "kind": kind,
    }


class AlertPipelineTests(unittest.TestCase):
    def test_digest_contains_every_active_match_once_and_mentions_once(self):
        snapshots = [
            SourceSnapshot(
                "Findbolig",
                listings=[
                    rental("find:1", "Store Kongensgade 1, 1264 Kobenhavn K", 14000, "Findbolig"),
                    rental("find:2", "Osterbrogade 2, 2100 Kobenhavn O", 17000, "Findbolig"),
                    rental(
                        "find:reserved",
                        "Osterbrogade 4, 2100 Kobenhavn O",
                        16000,
                        "Findbolig",
                        status="Reserved",
                    ),
                ],
            ),
            SourceSnapshot("RLE", events=[readiness("readiness:rle", "RLE", "empty")]),
        ]
        seen = {}
        payloads = []

        with patch.object(watcher, "WEBHOOK_URL", "https://discord.test/webhook"), patch.object(
            watcher, "DISCORD_MENTION_EVERYONE", "true"
        ), patch.object(
            watcher, "post_discord_payload", side_effect=lambda payload: payloads.append(payload) or True
        ), patch.object(watcher, "save_seen_states"):
            incomplete, delivery_failures = watcher.initialize_source_baselines(
                snapshots, {"Findbolig", "RLE"}, seen, max_chars=220
            )

        body = "\n".join(payload["content"] for payload in payloads)
        self.assertEqual(set(), incomplete)
        self.assertEqual(0, delivery_failures)
        self.assertEqual(1, body.count("@everyone"))
        self.assertEqual(1, body.count("Store Kongensgade 1"))
        self.assertEqual(1, body.count("Osterbrogade 2"))
        self.assertNotIn("Osterbrogade 4", body)
        self.assertIn("RLE current status", body)
        self.assertEqual("Reserved", seen["find:reserved"])
        self.assertEqual("complete", seen[watcher.baseline_state_key("Findbolig")])

    def test_source_is_not_seeded_until_all_of_its_chunks_succeed(self):
        snapshot = SourceSnapshot(
            "Findbolig",
            listings=[
                rental("find:1", "Store Kongensgade 1, 1264 Kobenhavn K", 14000, "Findbolig"),
                rental("find:2", "Osterbrogade 2, 2100 Kobenhavn O", 17000, "Findbolig"),
            ],
        )
        seen = {}
        chunks = watcher.build_baseline_digest_chunks([snapshot], max_chars=115)
        responses = [True] * len(chunks)
        responses[-1] = False
        with patch.object(watcher, "WEBHOOK_URL", "https://discord.test/webhook"), patch.object(
            watcher, "post_discord_payload", side_effect=responses
        ), patch.object(watcher, "save_seen_states"):
            incomplete, delivery_failures = watcher.initialize_source_baselines(
                [snapshot], {"Findbolig"}, seen, max_chars=115
            )

        self.assertEqual({"Findbolig"}, incomplete)
        self.assertEqual(1, delivery_failures)
        self.assertNotIn("find:1", seen)
        self.assertNotIn(watcher.baseline_state_key("Findbolig"), seen)
        self.assertEqual("sent", seen[watcher.BASELINE_MENTION_STATE_KEY])
        self.assertTrue(any(key.startswith(watcher.BASELINE_CHUNK_STATE_PREFIX) for key in seen))

        retry_payloads = []
        with patch.object(watcher, "WEBHOOK_URL", "https://discord.test/webhook"), patch.object(
            watcher,
            "post_discord_payload",
            side_effect=lambda payload: retry_payloads.append(payload) or True,
        ), patch.object(watcher, "save_seen_states"):
            incomplete, delivery_failures = watcher.initialize_source_baselines(
                [snapshot], {"Findbolig"}, seen, max_chars=115
            )
        self.assertEqual(set(), incomplete)
        self.assertEqual(0, delivery_failures)
        self.assertEqual(1, len(retry_payloads))

    def test_later_first_success_is_a_source_specific_catch_up_digest(self):
        seen = {
            watcher.baseline_state_key("Findbolig"): "complete",
            watcher.BASELINE_MENTION_STATE_KEY: "sent",
        }
        snapshots = [
            SourceSnapshot(
                "Findbolig",
                listings=[
                    rental("find:1", "Kronprinsessegade 1, 1306 Kobenhavn K", 15000, "Findbolig")
                ],
            ),
            SourceSnapshot("RLE", events=[readiness("readiness:rle", "RLE", "empty")]),
        ]
        payloads = []
        with patch.object(watcher, "WEBHOOK_URL", "https://discord.test/webhook"), patch.object(
            watcher, "post_discord_payload", side_effect=lambda payload: payloads.append(payload) or True
        ), patch.object(watcher, "save_seen_states"):
            incomplete, delivery_failures = watcher.initialize_source_baselines(
                snapshots, {"Findbolig", "RLE"}, seen
            )

        self.assertEqual(set(), incomplete)
        self.assertEqual(0, delivery_failures)
        body = "\n".join(payload["content"] for payload in payloads)
        self.assertIn("RLE", body)
        self.assertNotIn("Kronprinsessegade", body)
        self.assertNotIn("@everyone", body)

    def test_cross_source_duplicate_uses_the_preferred_origin_in_digest(self):
        key = "rent:handelsvej 23 2 th 2450 kobenhavn sv"
        aggregator = rental(
            "kobenhavn:1", "Handelsvej 23, 2. th., 2450 Kobenhavn SV", 12000, "Kobenhavn.dk", canonical_key=key
        )
        aggregator["source_priority"] = 40
        origin = rental(
            "brikk:1", "Handelsvej 23, 2. th., 2450 Kobenhavn SV", 12000, "Origin", canonical_key=key
        )
        origin["source_priority"] = 10
        prepared = watcher.prepare_source_snapshots(
            [SourceSnapshot("Kobenhavn.dk", [aggregator]), SourceSnapshot("Origin", [origin])]
        )
        self.assertEqual([], prepared[0].listings)
        self.assertEqual(["brikk:1"], [item["id"] for item in prepared[1].listings])

    def test_readiness_signature_change_alerts_and_persists_only_after_success(self):
        event = readiness("readiness:cphhomes", "CPH Homes", "revision-2")
        seen = {
            watcher.readiness_state_key(event["id"]): {
                "signature": "revision-1",
                "registration_closed": False,
                "signals": [],
            }
        }
        with patch.object(watcher, "send_readiness_notification", return_value=False) as send:
            sent, failures = watcher.process_readiness_events([event], seen)
        self.assertEqual((0, 1), (sent, failures))
        self.assertEqual("revision-1", seen[watcher.readiness_state_key(event["id"])]["signature"])
        send.assert_called_once()

    def test_registration_closed_to_open_transition_is_urgent(self):
        event = readiness("readiness:vaernedamsvej", "Den Franske Skole/Vaernedamsvej", "open", closed=False)
        event.update(
            {
                "application_url": "https://example.test/apply",
                "urgent_headline": "APPLICATION OPENING — Værnedamsvej",
            }
        )
        key = watcher.readiness_state_key(event["id"])
        seen = {key: {"signature": "closed", "registration_closed": True, "signals": []}}
        with patch.object(watcher, "send_readiness_notification", return_value=True) as send, patch.object(
            watcher, "save_seen_states"
        ):
            sent, failures = watcher.process_readiness_events([event], seen)
        self.assertEqual((1, 0), (sent, failures))
        self.assertTrue(send.call_args.kwargs["urgent"])
        self.assertEqual("application_opening", send.call_args.args[0]["kind"])
        self.assertEqual("https://example.test/apply", send.call_args.args[0]["url"])
        self.assertFalse(seen[key]["registration_closed"])

    def test_routine_readiness_has_no_ping_and_inspection_is_not_called_application_opening(self):
        captured = []
        inspection = readiness(
            "readiness:cphhomes:171",
            "CPH Homes",
            "changed",
            urgent=True,
            kind="inspection",
        )
        with patch.object(watcher, "WEBHOOK_URL", "https://discord.test/webhook"), patch.object(
            watcher, "post_discord_payload", side_effect=lambda payload: captured.append(payload) or True
        ):
            self.assertTrue(watcher.send_readiness_notification(inspection, urgent=True))
        self.assertNotIn("@everyone", captured[0]["content"])
        self.assertIn("Inspection needed", captured[0]["content"])
        self.assertNotIn("APPLICATION OPENING", captured[0]["content"])

    def test_existing_cph_signal_does_not_make_unrelated_revision_urgent(self):
        event = readiness(
            "readiness:cphhomes:171",
            "CPH Homes",
            "revision-2",
            signals=("term:husleje",),
            kind="inspection",
        )
        key = watcher.readiness_state_key(event["id"])
        seen = {key: {"signature": "revision-1", "registration_closed": False, "signals": ["term:husleje"]}}
        with patch.object(watcher, "send_readiness_notification", return_value=True) as send, patch.object(
            watcher, "save_seen_states"
        ):
            watcher.process_readiness_events([event], seen)
        self.assertFalse(send.call_args.kwargs["urgent"])

    def test_true_application_opening_mentions_once(self):
        captured = []
        opening = readiness(
            "readiness:vaernedamsvej",
            "Den Franske Skole/Vaernedamsvej",
            "open",
            urgent=True,
            kind="application_opening",
        )
        with patch.object(watcher, "WEBHOOK_URL", "https://discord.test/webhook"), patch.object(
            watcher, "DISCORD_MENTION_EVERYONE", "true"
        ), patch.object(watcher, "post_discord_payload", side_effect=lambda payload: captured.append(payload) or True):
            self.assertTrue(watcher.send_readiness_notification(opening, urgent=True))
        self.assertEqual(1, captured[0]["content"].count("@everyone"))
        self.assertIn("APPLICATION OPENING", captured[0]["content"])

    def test_parseable_rle_vacancy_sends_listing_only(self):
        listing = rental("rle:home", "Nørrebrogade 10, 2200 København N", 17500, "RLE")
        snapshots = [SourceSnapshot("RLE", listings=[listing], events=[])]
        specs = [SourceSpec("RLE", "ten_minute", lambda: snapshots[0])]
        seen = {watcher.baseline_state_key("RLE"): "complete"}
        with patch.object(watcher, "send_discord_notification", return_value=True) as home_alert, patch.object(
            watcher, "send_readiness_notification"
        ) as readiness_alert, patch.object(watcher, "save_seen_states"):
            sent, failures, incomplete = watcher.process_source_snapshots(snapshots, specs, seen)
        self.assertEqual((1, 0, set()), (sent, failures, incomplete))
        home_alert.assert_called_once()
        readiness_alert.assert_not_called()

    def test_canonical_state_suppresses_cross_run_source_replay_in_both_orders(self):
        key = "rent:handelsvej 23 2 th 2450 kobenhavn sv"
        origin = rental(
            "origin:1", "Handelsvej 23, 2. th., 2450 Kobenhavn SV", 12000, "Origin", canonical_key=key
        )
        aggregator = rental(
            "kobenhavn:1", "Handelsvej 23, 2. th., 2450 Kobenhavn SV", 12000, "Kobenhavn.dk", canonical_key=key
        )
        for first, second in ((origin, aggregator), (aggregator, origin)):
            seen = {}
            with self.subTest(first=first["source"]), patch.object(
                watcher, "send_discord_notification", return_value=True
            ) as send, patch.object(watcher, "save_seen_states"):
                self.assertEqual((1, 0), watcher.process_apartments([first], seen))
                self.assertEqual((0, 0), watcher.process_apartments([second], seen))
                send.assert_called_once()

    def test_new_reserved_is_seeded_silently_then_available_alerts(self):
        reserved = rental("lej:1", "Amagerbrogade 1, 2300 Kobenhavn S", 13000, "Lej", status="Reserved")
        available = dict(reserved, status="Available")
        seen = {}
        with patch.object(watcher, "send_discord_notification", return_value=True) as send, patch.object(
            watcher, "save_seen_states"
        ):
            self.assertEqual((0, 0), watcher.process_apartments([reserved], seen))
            send.assert_not_called()
            self.assertEqual((1, 0), watcher.process_apartments([available], seen))
            send.assert_called_once()

    def test_existing_raw_id_state_is_migrated_to_canonical_key_without_replay(self):
        listing = rental(
            "origin:legacy",
            "Nørrebrogade 1, 2200 København N",
            12000,
            "Origin",
            canonical_key="rent:norrebrogade 1 2200 kobenhavn n",
        )
        seen = {"origin:legacy": "Available"}
        with patch.object(watcher, "send_discord_notification") as send, patch.object(watcher, "save_seen_states"):
            self.assertEqual((0, 0), watcher.process_apartments([listing], seen))
        self.assertEqual("Available", seen[watcher.listing_state_key(listing)])
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
