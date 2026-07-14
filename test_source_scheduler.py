import unittest
from unittest.mock import patch

import watcher
from housing_sources import SourceSnapshot, SourceSpec


class SourceSchedulerTests(unittest.TestCase):
    def test_registry_assigns_every_new_source_to_the_approved_cadence(self):
        cadences = {spec.name: spec.cadence for spec in watcher.make_source_registry()}
        self.assertEqual("fast", cadences["Findbolig"])
        self.assertEqual("fast", cadences["Lejeboligmægleren"])
        self.assertEqual("fast", cadences["Norhjem"])
        for source in ("Taurus", "Brikk", "Kobenhavn.dk", "RLE", "Værnedamsvej"):
            self.assertEqual("ten_minute", cadences[source])
        self.assertEqual("thirty_minute", cadences["CPH Homes"])

    def test_fast_runs_each_cycle_but_fixed_source_waits_until_due(self):
        calls = []
        registry = [
            SourceSpec("Fast", "fast", lambda: calls.append("fast") or SourceSnapshot("Fast"), baseline=False),
            SourceSpec("Ten", "ten_minute", lambda: calls.append("ten") or SourceSnapshot("Ten")),
        ]
        due = {}
        first, first_success = watcher.fetch_due_sources(registry, now=100.0, next_due=due)
        second, second_success = watcher.fetch_due_sources(registry, now=101.0, next_due=due)
        self.assertEqual(["fast", "ten", "fast"], calls)
        self.assertEqual({"Fast", "Ten"}, first_success)
        self.assertEqual({"Fast"}, second_success)
        self.assertEqual(["Fast", "Ten"], [snapshot.source for snapshot in first])
        self.assertEqual(["Fast"], [snapshot.source for snapshot in second])

    def test_failure_is_isolated_and_zero_results_count_as_success(self):
        def fail():
            raise RuntimeError("source unavailable")

        registry = [
            SourceSpec("Broken", "ten_minute", fail),
            SourceSpec("Empty", "ten_minute", lambda: SourceSnapshot("Empty")),
        ]
        snapshots, succeeded = watcher.fetch_due_sources(registry, now=0.0, next_due={})
        self.assertEqual(["Empty"], [snapshot.source for snapshot in snapshots])
        self.assertEqual({"Empty"}, succeeded)

    def test_snapshot_source_must_match_registry_name(self):
        registry = [SourceSpec("Expected", "fast", lambda: SourceSnapshot("Wrong"))]
        snapshots, succeeded = watcher.fetch_due_sources(registry, now=0.0, next_due={})
        self.assertEqual([], snapshots)
        self.assertEqual(set(), succeeded)

    def test_baseline_runs_before_individual_alerts(self):
        listing = {
            "id": "find:1",
            "status": "Available",
            "name": "Store Kongensgade 1, 1264 Kobenhavn K",
            "price": {"amount": 14000},
            "location": {"formatted": "Store Kongensgade 1, 1264 Kobenhavn K"},
            "url": "https://example.test/find:1",
            "source": "Findbolig",
            "transaction_type": "rent",
        }
        snapshots = [SourceSnapshot("Findbolig", [listing])]
        specs = [SourceSpec("Findbolig", "fast", lambda: snapshots[0])]
        seen = {}
        with patch.object(
            watcher,
            "initialize_source_baselines",
            side_effect=lambda _s, _n, state: state.update(
                {"find:1": "Available", watcher.baseline_state_key("Findbolig"): "complete"}
            )
            or (set(), 0),
        ) as baseline, patch.object(watcher, "send_discord_notification") as individual:
            sent, failures, incomplete = watcher.process_source_snapshots(snapshots, specs, seen)
        self.assertEqual((0, 0, set()), (sent, failures, incomplete))
        baseline.assert_called_once()
        individual.assert_not_called()

    def test_akf_uses_logical_baseline_without_replaying_other_propstep_homes(self):
        normal = {
            "id": "propstep:normal",
            "status": "Available",
            "name": "Nørrebrogade 1, 2200 København N",
            "price": {"amount": 12000},
            "location": {"formatted": "Nørrebrogade 1, 2200 København N"},
            "url": "https://propstep.test/normal",
            "source": "Propstep",
            "transaction_type": "rent",
        }
        akf = {
            **normal,
            "id": "propstep:akf",
            "name": "Nørrebrogade 2, 2200 København N",
            "location": {"formatted": "Nørrebrogade 2, 2200 København N"},
            "url": "https://propstep.test/akf",
            "source": "AKF via Propstep",
        }
        snapshots = [SourceSnapshot("Propstep", [normal, akf])]
        specs = [SourceSpec("Propstep", "fast", lambda: snapshots[0], baseline=False)]
        seen = {"propstep:normal": "Available"}
        payloads = []
        with patch.object(watcher, "WEBHOOK_URL", "https://discord.test/webhook"), patch.object(
            watcher, "post_discord_payload", side_effect=lambda payload: payloads.append(payload) or True
        ), patch.object(watcher, "send_discord_notification") as individual, patch.object(watcher, "save_seen_states"):
            sent, failures, incomplete = watcher.process_source_snapshots(snapshots, specs, seen)
        body = "\n".join(payload["content"] for payload in payloads)
        self.assertEqual((0, 0, set()), (sent, failures, incomplete))
        self.assertIn("Nørrebrogade 2", body)
        self.assertNotIn("Nørrebrogade 1", body)
        self.assertEqual("complete", seen[watcher.baseline_state_key("AKF via Propstep")])
        individual.assert_not_called()

    def test_failed_baseline_is_counted_and_suppresses_individual_flood(self):
        listing = {
            "id": "find:1",
            "status": "Available",
            "name": "Store Kongensgade 1, 1264 København K",
            "price": {"amount": 14000},
            "location": {"formatted": "Store Kongensgade 1, 1264 København K"},
            "url": "https://example.test/find:1",
            "source": "Findbolig",
            "transaction_type": "rent",
        }
        snapshots = [SourceSnapshot("Findbolig", [listing])]
        specs = [SourceSpec("Findbolig", "fast", lambda: snapshots[0])]
        with patch.object(watcher, "initialize_source_baselines", return_value=({"Findbolig"}, 1)), patch.object(
            watcher, "send_discord_notification"
        ) as individual:
            sent, failures, incomplete = watcher.process_source_snapshots(snapshots, specs, {})
        self.assertEqual((0, 1, {"Findbolig"}), (sent, failures, incomplete))
        individual.assert_not_called()


if __name__ == "__main__":
    unittest.main()
