"""Seed eligibility is distinct from client eligibility; synthetic records only."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from test_leads import candidate, leads, source


class SeedPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.path = self.base / "synthetic.sqlite"
        self.db = leads.connect(self.path, initialize=True)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add(self, *values):
        leads.ingest(self.db, {"accounts": list(values)})

    def review(self, batch, number=1, decision="rejected", failed=None, reason="Synthetic human feedback"):
        return leads.review(self.db, {"batch_id": batch["batch_id"], "reviews": [{
            "number": number, "decision": decision, "failed_criteria": failed or [], "reason": reason}]})

    def views(self):
        return {a["username"]: a for a in leads.account_views(self.db)}

    def table(self, name):
        return [tuple(row) for row in self.db.execute(f"SELECT * FROM {name} ORDER BY rowid")]

    def test_policy_is_explicit_idempotent_and_does_not_generate_human_feedback(self):
        self.add(candidate("fixture_female", strong=False))
        batch = leads.make_batch(self.db, 20)
        self.review(batch, failed=["male"])
        frozen = {name: self.table(name) for name in ("accounts", "reviews", "batches", "batch_items", "rules", "mode_events")}
        before_changes = self.db.total_changes
        self.assertEqual(leads.current_seed_policy(self.db), {"allow_female": False})
        self.assertFalse(leads.set_seed_policy(self.db)["changed"])
        self.assertEqual(self.db.total_changes, before_changes)
        for value in (True, False):
            with self.assertRaisesRegex(ValueError, "reason"):
                leads.set_seed_policy(self.db, value, "  ")
        enabled = leads.set_seed_policy(self.db, True, "User permits female discovery seeds")
        self.assertTrue(enabled["changed"])
        self.assertEqual(enabled["events"][0]["old_allow_female"], False)
        self.assertEqual(enabled["events"][0]["new_allow_female"], True)
        self.assertFalse(leads.set_seed_policy(self.db, True, "Repeated explicit choice")["changed"])
        self.assertEqual(len(leads.set_seed_policy(self.db)["events"]), 1)
        self.assertEqual(frozen, {name: self.table(name) for name in frozen})
        self.assertEqual(leads.current_mode(self.db), "learning")

    def test_only_gender_only_human_rejections_become_seeds_and_never_clients(self):
        values = [candidate(f"fixture_{name}", strong=False) for name in
                  ("client", "female", "business", "mixed", "unspecified", "uncertain", "model_female")]
        values[-1]["assessment"]["male"] = "no"
        self.add(*values)
        batch = leads.make_batch(self.db, 20)
        decisions = [("accepted", []), ("rejected", ["male"]), ("rejected", ["personal"]),
                     ("rejected", ["male", "personal"]), ("rejected", []), ("uncertain", ["male"])]
        for number, (decision, failed) in enumerate(decisions, 1):
            self.review(batch, number, decision, failed)
        self.assertEqual([a["username"] for a in leads.next_accounts(self.db, 20)["accounts"]], ["fixture_client"])
        leads.set_seed_policy(self.db, True, "Explicit user policy")
        views = self.views()
        self.assertEqual({a["username"] for a in views.values() if a["seed_eligible"]}, {"fixture_client", "fixture_female"})
        self.assertEqual(views["fixture_client"]["seed_origin"], "accepted_client")
        self.assertFalse(views["fixture_client"]["seed_only"])
        self.assertEqual(views["fixture_female"]["seed_origin"], "human_female_feedback")
        self.assertTrue(views["fixture_female"]["seed_only"])
        following = {a["username"]: a for a in leads.next_accounts(self.db, 20)["accounts"]}
        self.assertEqual(following["fixture_female"]["client_status"], "rejected")
        self.assertTrue(following["fixture_female"]["seed_only"])
        report = self.base / "accepted.json"
        self.assertEqual(leads.export_accounts(self.db, report, "json")["count"], 1)
        self.assertEqual([a["username"] for a in json.loads(report.read_text())], ["fixture_client"])
        self.assertEqual(leads.stats(self.db)["seed_only_accounts"], ["fixture_female"])
        self.assertEqual(leads.stats(self.db)["seed_eligible_count"], 2)
        self.db.commit()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(leads.main(["--db", str(self.path), "list", "--seeds", "--status", "rejected"]), 0)
        self.assertEqual([a["username"] for a in json.loads(output.getvalue())], ["fixture_female"])

    def test_female_relation_counts_once_and_does_not_bypass_candidate_evidence(self):
        for name in ("fixture_client_one", "fixture_client_two"):
            self.add(candidate(name))
            self.review(leads.make_batch(self.db, 1), decision="accepted")
        self.add(candidate("fixture_female", strong=False))
        self.review(leads.make_batch(self.db, 1), failed=["male"])
        leads.set_mode(self.db, "assisted", "Two complete synthetic human batches")
        self.add(candidate("fixture_unknown", strong=False, sources=[
            source("fixture_client_one"), source("fixture_female"), source("fixture_female", "follower"),
            source("fixture_female", "comment", "https://www.instagram.com/p/FIXTURE/"),
            source("fixture_unknown"), source("fixture_female", "mention", "fixture mention")]))
        self.assertEqual(self.views()["fixture_unknown"]["independent_seed_count"], 1)
        review_count = len(self.table("reviews"))
        leads.set_seed_policy(self.db, True, "Allow female discovery seeds")
        view = self.views()["fixture_unknown"]
        self.assertEqual(view["independent_seeds"], ["fixture_client_one", "fixture_female"])
        self.assertEqual(view["status"], "pending")
        self.assertFalse(view["seed_eligible"])
        self.assertEqual(len(self.table("reviews")), review_count)
        self.assertEqual(leads.current_mode(self.db), "assisted")
        leads.set_seed_policy(self.db, False, "User disables female discovery seeds")
        self.assertEqual(self.views()["fixture_unknown"]["independent_seed_count"], 1)
        self.assertEqual(self.views()["fixture_female"]["status"], "rejected")

    def test_latest_feedback_revokes_seed_only_across_batches(self):
        self.add(candidate("fixture_female", strong=False))
        old_batch = leads.make_batch(self.db, 1)
        self.review(old_batch, failed=["male"])
        leads.set_seed_policy(self.db, True, "Explicit policy")
        self.assertTrue(self.views()["fixture_female"]["seed_only"])
        correction = leads.make_batch(self.db, 1, include_reviewed=True)
        self.review(correction, failed=["personal"], reason="Human correction: business account")
        self.assertFalse(self.views()["fixture_female"]["seed_eligible"])
        self.assertEqual(leads.next_accounts(self.db, 20)["accounts"], [])
        self.assertEqual(self.review(old_batch, failed=["male"])["recorded_reviews"], 0)
        self.assertFalse(self.views()["fixture_female"]["seed_eligible"])
        for decision, failed in (("uncertain", ["male"]), ("rejected", []), ("rejected", ["male", "resident_hk"])):
            self.review(correction, decision=decision, failed=failed)
            self.assertFalse(self.views()["fixture_female"]["seed_eligible"])
        self.review(correction, failed=["male"])
        self.assertTrue(self.views()["fixture_female"]["seed_only"])
        self.review(correction, decision="accepted")
        self.assertTrue(self.views()["fixture_female"]["seed_eligible"])
        self.assertFalse(self.views()["fixture_female"]["seed_only"])

    def test_cli_persists_policy_and_requires_a_reason(self):
        self.db.commit()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(leads.main(["--db", str(self.path), "seed-policy", "--allow-female"]), 2)
        for command in (["--allow-female", "--reason", "Explicit user instruction"], [],
                        ["--allow-female", "--reason", "Repeated instruction"],
                        ["--disallow-female", "--reason", "User changed policy"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(leads.main(["--db", str(self.path), "seed-policy", *command]), 0)
        result = leads.set_seed_policy(self.db)
        self.assertFalse(result["allow_female"])
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(result["events"][-1]["reason"], "User changed policy")

    def test_v2_upgrade_preserves_existing_data_and_defaults_to_disabled(self):
        self.add(candidate("fixture_legacy", strong=False))
        batch = leads.make_batch(self.db, 1)
        self.review(batch, failed=["male"])
        leads.write_progress(self.db, {"entries": [{"username": "fixture_legacy", "kind": "following", "status": "in_progress", "cursor": {"seen": 4}}]})
        leads.learn(self.db, {"based_on_batches": [batch["batch_id"]], "reason": "Synthetic feedback", "rules": ["Synthetic rule"]})
        self.db.execute("DROP TABLE seed_policy_events")
        self.db.execute("DELETE FROM meta WHERE key='seed_allow_female'")
        self.db.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        # Version 2 batches did not contain the new derived seed fields.
        snapshot = json.loads(self.db.execute("SELECT snapshot_json FROM batch_items").fetchone()[0])
        for key in ("seed_eligible", "seed_only", "seed_origin"):
            snapshot.pop(key)
        self.db.execute("UPDATE batch_items SET snapshot_json=?", (leads.dump(snapshot),))
        tables = ("accounts", "reviews", "assessments", "evidence", "sources", "batches", "batch_items", "rules", "mode_events", "progress", "progress_events")
        before = {name: self.table(name) for name in tables}
        self.db.commit()
        self.db.close()
        with self.assertRaisesRegex(ValueError, "旧版本 2"):
            leads.connect(self.path)
        self.db = leads.connect(self.path, allow_migration=True)
        self.assertEqual(self.db.migrations_applied, ["2->3"])
        self.assertEqual(before, {name: self.table(name) for name in tables})
        self.assertEqual(leads.current_seed_policy(self.db), {"allow_female": False})
        self.assertEqual(leads.set_seed_policy(self.db)["events"], [])
        self.assertFalse(self.views()["fixture_legacy"]["seed_eligible"])
        leads.set_seed_policy(self.db, True, "Explicit instruction after upgrade")
        self.assertTrue(self.views()["fixture_legacy"]["seed_only"])
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_rename_and_forget_update_female_seed_and_child_references(self):
        self.add(candidate("fixture_old", strong=False))
        self.review(leads.make_batch(self.db, 1), failed=["male"])
        self.add(candidate("fixture_child", strong=False, sources=[source("fixture_old")]))
        child_batch = leads.make_batch(self.db, 1)
        leads.set_seed_policy(self.db, True, "Explicit policy")
        leads.merge(self.db, "fixture_old", "fixture_new", "Observed rename")
        views = self.views()
        self.assertTrue(views["fixture_new"]["seed_only"])
        self.assertEqual(views["fixture_child"]["independent_seeds"], ["fixture_new"])
        historical = leads.batch_view(self.db, child_batch["batch_id"])["items"][0]["snapshot"]
        self.assertEqual(historical["sources"][0]["source_account"], "fixture_new")
        # A rename preserves the actually observed old URL; it is not a fresh browser observation.
        self.assertEqual(historical["sources"][0]["source_url"], "https://www.instagram.com/fixture_old/")
        leads.forget(self.db, "fixture_new", "Synthetic deletion request")
        self.assertEqual(self.views()["fixture_child"]["independent_seed_count"], 0)
        self.assertEqual(leads.stats(self.db)["seed_only_accounts"], [])
        self.assertNotIn("fixture_new", json.dumps(leads.batch_view(self.db, child_batch["batch_id"])))
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_merge_uses_latest_rejection_reason_and_eligibility_in_either_direction(self):
        leads.set_seed_policy(self.db, True, "Explicit policy")
        for reverse in (False, True):
            prefix = "fixture_reverse" if reverse else "fixture_forward"
            female, business, child = prefix + "_f", prefix + "_b", prefix + "_c"
            self.add(candidate(female, strong=False))
            self.review(leads.make_batch(self.db, 1), failed=["male"], reason="Earlier female feedback")
            self.add(candidate(business, strong=False))
            self.review(leads.make_batch(self.db, 1), failed=["personal"], reason="Later business correction")
            self.add(candidate(child, strong=False, sources=[source(female), source(business)]))
            self.assertEqual(self.views()[child]["independent_seed_count"], 1)
            old, new = (business, female) if reverse else (female, business)
            leads.merge(self.db, old, new, "Same account synthetic merge")
            view = self.views()[new]
            self.assertEqual(view["status"], "rejected")
            self.assertEqual(view["manual_reason"], "Later business correction")
            self.assertFalse(view["seed_eligible"])
            self.assertEqual(self.views()[child]["independent_seed_count"], 0)
            self.assertEqual(len(self.views()[child]["sources"]), 1)
            # Exclude this pending child from the next fixture's batch selection.
            self.review(leads.make_batch(self.db, 1), decision="uncertain")
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
