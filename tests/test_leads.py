"""Behavior tests using synthetic records only; no network or real account reads."""

import contextlib
import copy
import csv
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "leads.py"
SPEC = importlib.util.spec_from_file_location("leads", SCRIPT)
leads = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(leads)
AT = "2026-09-05T09:00:00+08:00"


def candidate(name, *, strong=True, residence_signal="self_description", sources=None):
    """Names and quoted evidence are test fixtures, not verified people."""
    return {
        "username": name,
        "assessment": {"personal": "yes", "male": "yes", "resident_hk": "yes" if strong else "unknown",
                       "confidence": "clear" if strong else "uncertain", "reason": "Synthetic fixture evidence"},
        "evidence": [{"criterion": criterion, "signal": residence_signal if criterion == "resident_hk" else "self_description",
                      "text": "Synthetic fixture only", "url": f"https://www.instagram.com/{name}/", "observed_at": AT}
                     for criterion in leads.CRITERIA],
        "sources": sources or [],
    }


def source(seed, kind="following", key=None):
    return {"kind": kind, "source_account": seed, "source_url": f"https://www.instagram.com/{seed}/",
            "source_key": key or kind, "observed_at": AT}


class LeadsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.path = self.base / "test.sqlite"
        self.db = leads.connect(self.path, initialize=True)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add(self, *accounts):
        result = leads.ingest(self.db, {"accounts": list(accounts)})
        self.db.commit()
        return result

    def views(self):
        return {item["username"]: item for item in leads.account_views(self.db)}

    def feedback(self, batch, decision="符合", reason="Actual human feedback fixture"):
        result = leads.review(self.db, {"batch_id": batch["batch_id"], "reviews": [
            {"number": item["number"], "decision": decision, "reason": reason} for item in batch["items"]]})
        self.db.commit()
        return result

    def prepare_assisted(self):
        for name in ("fixture_seed_one", "fixture_seed_two"):
            self.add(candidate(name))
            self.feedback(leads.make_batch(self.db, 1))
        leads.set_mode(self.db, "assisted", "Two complete human feedback batches")
        self.db.commit()

    def test_duplicate_ingest_normalizes_accounts_and_logical_sources(self):
        account = candidate("fixture_candidate", sources=[source("fixture_seed")])
        account["username"] = "@FIXTURE_CANDIDATE"
        account["profile_url"] = "https://instagram.com/FIXTURE_CANDIDATE/?hl=en"
        first = self.add(account)
        second = self.add(account)
        self.assertEqual(first["inserted_accounts"], 1)
        self.assertEqual(second, {"inserted_accounts": 0, "updated_assessments": 0, "new_sources": 0, "new_evidence": 0,
                                  "new_auto_acceptances": 0, "mode": "learning"})
        later = copy.deepcopy(account)
        later["sources"][0].update(source_key="another_button_label", observed_at="2026-09-06T01:00:00Z")
        self.add(later)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assessments").fetchone()[0], 1)
        self.assertEqual(self.views()["fixture_candidate"]["profile_url"], "https://www.instagram.com/fixture_candidate/")
        self.assertEqual(self.views()["fixture_candidate"]["status"], "pending")

    def test_independent_confirmed_seeds_not_raw_sightings_control_priority(self):
        self.prepare_assisted()
        self.add(candidate("fixture_unconfirmed_seed", strong=False))
        one = candidate("fixture_one_source", strong=False, sources=[source("fixture_seed_one")])
        many_same = candidate("fixture_same_source", strong=False, sources=[
            source("fixture_seed_one"), source("fixture_seed_one", "follower"),
            source("fixture_seed_one", "comment", "https://www.instagram.com/p/FIXTURE1/"),
            source("fixture_seed_one", "comment", "https://www.instagram.com/p/FIXTURE2/"),
            source("fixture_unconfirmed_seed"), source("fixture_same_source")])
        self.add(one, many_same)
        self.assertEqual(self.views()["fixture_same_source"]["independent_seed_count"], 1)
        self.assertEqual(len(self.views()["fixture_same_source"]["sources"]), 6)
        self.add({"username": "fixture_same_source", "sources": [source("fixture_seed_two")]})
        view = self.views()["fixture_same_source"]
        self.assertEqual(view["independent_seed_count"], 2)
        self.assertEqual(view["status"], "pending")
        self.assertEqual(leads.account_views(self.db)[0]["username"], "fixture_same_source")
        kinds = {row[0] for row in self.db.execute("SELECT kind FROM sources WHERE username='fixture_same_source'")}
        self.assertTrue({"following", "follower", "comment"}.issubset(kinds))

    def test_manual_rejection_uncertainty_and_acceptance_survive_ingest(self):
        cases = (("fixture_rejected", "不符合", "rejected"), ("fixture_uncertain", "不确定", "pending"),
                 ("fixture_accepted", "符合", "accepted"))
        for name, decision, expected in cases:
            self.add(candidate(name, strong=False))
            self.feedback(leads.make_batch(self.db, 1), decision)
        for name, decision, expected in cases:
            self.add(candidate(name, strong=decision != "符合", sources=[source("fixture_accepted")]))
            self.assertEqual(self.views()[name]["status"], expected)
            self.assertEqual(self.views()[name]["decision_origin"], "human")
        leads.set_mode(self.db, "automatic", "Three batches of explicit human feedback")
        self.assertEqual(self.views()["fixture_rejected"]["status"], "rejected")
        self.assertEqual(self.views()["fixture_uncertain"]["status"], "pending")
        names = {item["username"] for item in leads.next_accounts(self.db, 20)["accounts"]}
        self.assertEqual(names, {"fixture_accepted"})

    def test_unknown_flag_language_and_missing_evidence_cannot_auto_pass(self):
        self.prepare_assisted()
        values = [candidate("fixture_unknown", strong=False), candidate("fixture_flag", residence_signal="flag"),
                  candidate("fixture_language", residence_signal="language"), candidate("fixture_checkin", residence_signal="location_tag"),
                  candidate("fixture_missing"), candidate("fixture_good"), candidate("fixture_flag_alias", residence_signal="hk_flag"),
                  candidate("fixture_typo", residence_signal="explict_residance")]
        values[4]["evidence"] = values[4]["evidence"][:2]
        self.add(*values)
        for name in ("fixture_unknown", "fixture_flag", "fixture_language", "fixture_checkin", "fixture_missing", "fixture_flag_alias", "fixture_typo"):
            self.assertEqual(self.views()[name]["status"], "pending", name)
            self.assertFalse(self.views()[name]["model_eligible"])
        self.assertEqual(self.views()["fixture_good"]["status"], "accepted")
        self.assertEqual(self.views()["fixture_good"]["decision_origin"], "automatic")
        # A new all-yes assessment cannot silently borrow previously supplied evidence.
        self.add({"username": "fixture_good", "assessment": candidate("fixture_good")["assessment"]})
        self.assertEqual(self.views()["fixture_good"]["status"], "pending")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM evidence WHERE username='fixture_good'").fetchone()[0], 3)

    def test_batch_numbers_and_model_snapshots_are_stable(self):
        self.add(candidate("fixture_original", strong=False), candidate("fixture_second"))
        batch = leads.make_batch(self.db, 20)
        before = json.dumps(batch["items"], sort_keys=True)
        self.add(candidate("fixture_new"), candidate("fixture_original"))
        after = leads.batch_view(self.db, batch["batch_id"])
        self.assertEqual(before, json.dumps(after["items"], sort_keys=True))
        self.assertFalse(after["items"][0]["snapshot"]["model_eligible"])
        new_batch = leads.make_batch(self.db, 20)
        self.assertEqual([item["username"] for item in new_batch["items"]], ["fixture_new"])
        self.feedback(batch)
        stats = leads.stats(self.db)["model_snapshot_vs_human"]
        self.assertEqual(stats["ineligible_accepted"], 1)
        self.assertEqual(stats["eligible_accepted"], 1)

    def test_review_corrections_append_history_and_repeated_import_is_idempotent(self):
        self.add(candidate("fixture_candidate"))
        batch = leads.make_batch(self.db, 20)
        self.feedback(batch, "不符合", "First user conclusion")
        self.feedback(batch, "不符合", "First user conclusion")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 1)
        self.feedback(batch, "符合", "User explicitly corrected the conclusion")
        history = leads.batch_view(self.db, batch["batch_id"])["items"][0]["reviews"]
        self.assertEqual([entry["decision"] for entry in history], ["rejected", "accepted"])
        self.assertEqual(self.views()["fixture_candidate"]["status"], "accepted")
        review_batch = leads.make_batch(self.db, 20, include_reviewed=True)
        self.assertNotEqual(review_batch["batch_id"], batch["batch_id"])

    def test_next_is_read_only_and_progress_is_per_entry_and_post(self):
        self.add(candidate("fixture_candidate"))
        self.feedback(leads.make_batch(self.db, 20))
        before_changes = self.db.total_changes
        first = leads.next_accounts(self.db, 20)
        self.assertEqual(first, leads.next_accounts(self.db, 20))
        self.assertEqual(self.db.total_changes, before_changes)
        self.assertEqual({entry["kind"] for entry in first["accounts"][0]["entries"]}, {"following", "follower", "posts"})
        entries = [{"username": "fixture_candidate", "kind": kind, "status": "done", "cursor": "Visible scope completed"}
                   for kind in ("following", "follower", "posts")]
        entries += [{"username": "fixture_candidate", "kind": "comment", "source_key": f"https://instagram.com/p/{key}/?utm_source=test",
                     "status": state, "cursor": {"last_visible_comment": key}, "reason": "Visible comments only"}
                    for key, state in (("FIXTURE1", "done"), ("FIXTURE2", "in_progress"))]
        leads.write_progress(self.db, {"entries": entries})
        pending = leads.next_accounts(self.db, 20)["accounts"][0]["entries"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["source_key"], "https://www.instagram.com/p/FIXTURE2/")
        self.assertEqual(pending[0]["cursor"], {"last_visible_comment": "FIXTURE2"})
        final = entries[-1].copy()
        final.update(status="unavailable", reason="Page is no longer available")
        leads.write_progress(self.db, {"entries": [final]})
        self.assertEqual(leads.next_accounts(self.db, 20)["accounts"], [])
        unavailable = leads.next_accounts(self.db, 20, include_unavailable=True)["accounts"]
        self.assertEqual(unavailable[0]["unavailable_entries"][0]["source_key"], "https://www.instagram.com/p/FIXTURE2/")
        self.assertEqual(leads.stats(self.db)["unavailable_entries"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM progress_events WHERE kind='comment'").fetchone()[0], 3)
        with self.assertRaisesRegex(ValueError, "具体 Instagram"):
            leads.write_progress(self.db, {"entries": [{**final, "source_key": "https://www.instagram.com/fixture_candidate/"}]})

    def test_mode_needs_multiple_complete_human_batches_not_model_self_review(self):
        self.add(candidate("fixture_one"), candidate("fixture_two"))
        with self.assertRaisesRegex(ValueError, "至少 2"):
            leads.set_mode(self.db, "assisted", "All model judgments are clear")
        batch = leads.make_batch(self.db, 20)
        leads.review(self.db, {"batch_id": batch["batch_id"], "reviews": [{"number": 1, "decision": "符合", "reason": "User feedback"}]})
        self.assertEqual(leads.completed_batches(self.db), [])
        with self.assertRaisesRegex(ValueError, "至少 2"):
            leads.set_mode(self.db, "automatic", "One partial batch")
        self.feedback(batch)
        self.add(candidate("fixture_three"))
        self.feedback(leads.make_batch(self.db, 1))
        with self.assertRaisesRegex(ValueError, "reason"):
            leads.set_mode(self.db, "assisted", None)
        result = leads.set_mode(self.db, "assisted", "Two completed human batches")
        self.assertEqual(result["mode"], "assisted")
        self.assertEqual(len(result["completed_human_batches"]), 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM mode_events").fetchone()[0], 1)

    def test_learning_records_versions_with_human_batch_provenance(self):
        self.add(candidate("fixture_candidate"))
        batch = leads.make_batch(self.db, 20)
        payload = {"based_on_batches": [batch["batch_id"]], "rules": ["Read recent original posts"], "reason": "Human feedback summary"}
        with self.assertRaisesRegex(ValueError, "人工反馈"):
            leads.learn(self.db, payload)
        self.feedback(batch)
        self.assertEqual(leads.learn(self.db, payload)["version"], 1)
        revised = {**payload, "rules": ["Keep ambiguous cases pending"], "reason": "Second human feedback summary"}
        self.assertEqual(leads.learn(self.db, revised)["version"], 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM rules").fetchone()[0], 2)
        self.assertEqual(leads.stats(self.db)["latest_rule"]["rules"], revised["rules"])
        self.assertEqual(leads.current_mode(self.db), "learning")

    def test_partial_human_feedback_can_inform_rules_but_cannot_upgrade_mode(self):
        self.add(candidate("fixture_reviewed"), candidate("fixture_not_reviewed"))
        batch = leads.make_batch(self.db, 20)
        payload = {"based_on_batches": [batch["batch_id"]], "rules": ["Only actual received human feedback supports this rule"], "reason": "One user judgment already arrived"}
        with self.assertRaisesRegex(ValueError, "人工反馈"):
            leads.learn(self.db, payload)
        leads.review(self.db, {"batch_id": batch["batch_id"], "reviews": [{"number": 1, "decision": "符合"}]})
        self.assertEqual(leads.learn(self.db, payload)["version"], 1)
        self.assertEqual(leads.completed_batches(self.db), [])
        with self.assertRaisesRegex(ValueError, "至少 2"):
            leads.set_mode(self.db, "assisted", "Only one human review exists")
        self.assertEqual(self.views()["fixture_not_reviewed"]["decision_origin"], "model_pending")

    def test_csv_protects_formula_cells_and_exports_only_accepted_by_default(self):
        self.add(candidate("fixture_candidate"))
        batch = leads.make_batch(self.db, 20)
        formula = '  =HYPERLINK("https://example.invalid","fixture")'
        self.feedback(batch, reason=formula)
        self.add(candidate("fixture_pending", strong=False))
        output = self.base / "export.csv"
        result = leads.export_accounts(self.db, output, "csv")
        self.assertEqual(result["count"], 1)
        with output.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["reason"], "'" + formula)
        self.assertEqual(rows[0]["model_resident_hk"], "yes")
        self.assertNotIn("resident_hk", rows[0])
        for cell in ("=1+1", "+1", "-1", "@SUM(A1)", "\t=1", "\r=1", "\n=1", "  =1"):
            self.assertTrue(leads.csv_safe(cell).startswith("'"), cell)
        with self.assertRaisesRegex(ValueError, "已存在"):
            leads.export_accounts(self.db, output, "csv")
        all_json = self.base / "all.json"
        self.assertEqual(leads.export_accounts(self.db, all_json, "json", include_all=True)["count"], 2)
        self.assertEqual(len(json.loads(all_json.read_text(encoding="utf-8"))), 2)
        self.assertEqual(leads.export_accounts(self.db, self.base / "accepted.md", "markdown")["count"], 1)
        markdown = (self.base / "accepted.md").read_text(encoding="utf-8")
        self.assertIn("来源路径", markdown)
        self.assertIn("resident_hk / self_description", markdown)

    def test_review_can_record_number_only_feedback_without_inventing_reason(self):
        self.add(candidate("fixture_candidate"))
        batch = leads.make_batch(self.db, 20)
        leads.review(self.db, {"batch_id": batch["batch_id"], "reviews": [{"number": 1, "decision": "符合"}]})
        self.assertEqual(self.views()["fixture_candidate"]["manual_reason"], "")
        self.assertEqual(leads.batch_view(self.db, batch["batch_id"])["items"][0]["reviews"][0]["reason"], "")

    def test_init_refuses_foreign_databases_without_changing_bytes(self):
        foreign = self.base / "foreign.sqlite"
        with contextlib.closing(sqlite3.connect(foreign)) as db:
            with db:
                db.execute("CREATE TABLE important_data (value TEXT)")
                db.execute("INSERT INTO important_data VALUES ('keep this')")
        before = foreign.read_bytes()
        with self.assertRaisesRegex(ValueError, "未修改"):
            leads.connect(foreign, initialize=True)
        self.assertEqual(foreign.read_bytes(), before)
        with contextlib.closing(sqlite3.connect(foreign)) as db:
            self.assertEqual([row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")], ["important_data"])
        non_sqlite = self.base / "notes.txt"
        non_sqlite.write_text("Do not change this file")
        before_text = non_sqlite.read_bytes()
        with self.assertRaisesRegex(ValueError, "未修改"):
            leads.connect(non_sqlite, initialize=True)
        self.assertEqual(non_sqlite.read_bytes(), before_text)
        self.add(candidate("fixture_candidate"))
        reinitialized = leads.connect(self.path, initialize=True)
        self.assertEqual(reinitialized.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)
        reinitialized.close()

    def test_search_and_place_progress_survives_without_an_account(self):
        entries = [{"kind": "search", "source_key": "https://www.instagram.com/explore/search/keyword/?q=HongKong&igsh=tracking",
                    "status": "in_progress", "cursor": {"last_visible_result": "fixture"}},
                   {"kind": "search", "source_key": "https://instagram.com/explore/search/keyword/?q=Kowloon",
                    "status": "pending"},
                   {"kind": "place", "source_key": "Synthetic place key", "status": "unavailable", "reason": "Place entry is not visible"}]
        leads.write_progress(self.db, {"entries": entries})
        next_items = leads.next_accounts(self.db, 20)
        self.assertEqual(next_items["accounts"], [])
        self.assertEqual(len(next_items["discovery_entries"]), 2)
        keys = {item["source_key"] for item in next_items["discovery_entries"]}
        self.assertEqual(keys, {"https://www.instagram.com/explore/search/keyword/?q=HongKong", "https://www.instagram.com/explore/search/keyword/?q=Kowloon"})
        self.assertEqual(len(leads.next_accounts(self.db, 20, include_unavailable=True)["discovery_entries"]), 3)
        self.assertEqual(len(leads.stats(self.db)["discovery_progress"]), 3)
        unchanged = copy.deepcopy(entries[0])
        unchanged["source_key"] = "https://instagram.com/explore/search/keyword/?utm_source=different&q=HongKong"
        leads.write_progress(self.db, {"entries": [unchanged]})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM discovery_progress").fetchone()[0], 3)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM discovery_progress_events").fetchone()[0], 3)

    def test_business_url_parameters_preserved_tracking_does_not_add_sources(self):
        first = source("fixture_seed", "comment", "https://instagram.com/p/FIXTURE/?comment_id=123&igsh=track")
        second = source("fixture_seed", "comment", "https://www.instagram.com/p/FIXTURE/?utm_campaign=x&comment_id=123")
        self.add(candidate("fixture_candidate", sources=[first, second]))
        sources = self.views()["fixture_candidate"]["sources"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["source_key"], "https://www.instagram.com/p/FIXTURE/?comment_id=123")
        self.assertIn("keywords=hk", leads.url("https://www.instagram.com/explore/search/?keywords=hk"))

    def test_normal_batches_only_request_pending_accounts_auto_acceptance_needs_explicit_audit(self):
        self.prepare_assisted()
        self.add(candidate("fixture_auto_accepted"), candidate("fixture_pending", strong=False))
        regular = leads.make_batch(self.db, 20)
        self.assertEqual([item["username"] for item in regular["items"]], ["fixture_pending"])
        audit = leads.make_batch(self.db, 20, include_reviewed=True)
        self.assertIn("fixture_auto_accepted", [item["username"] for item in audit["items"]])
        self.assertNotIn("fixture_pending", [item["username"] for item in audit["items"]])

    def test_cli_transaction_rolls_back_invalid_multi_account_import(self):
        payload = self.base / "bad.json"
        payload.write_text(encoding="utf-8", data=json.dumps({"accounts": [candidate("fixture_valid"), {"username": "INVALID NAME"}]}))
        with contextlib.redirect_stderr(io.StringIO()):
            code = leads.main(["--db", str(self.path), "ingest", str(payload)])
        self.assertEqual(code, 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)

    def test_cli_requires_explicit_database_and_does_not_create_missing_read_database(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as captured:
            leads.main(["stats"])
        self.assertEqual(captured.exception.code, 2)
        missing = self.base / "missing.sqlite"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(leads.main(["--db", str(missing), "stats"]), 2)
        self.assertFalse(missing.exists())
        for command in ("stats", "list", "next", "mode"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(leads.main(["--db", str(self.path), command]), 0)
            self.assertIsInstance(json.loads(output.getvalue()), (dict, list))

    def test_batch_list_exposes_open_batches_and_pending_numbers_for_handover(self):
        self.assertEqual(leads.list_batches(self.db), {"batches": [], "open_batches": []})
        self.add(candidate("fixture_a", strong=False), candidate("fixture_b", strong=False), candidate("fixture_c", strong=False))
        first = leads.make_batch(self.db, 2)
        leads.review(self.db, {"batch_id": first["batch_id"], "reviews": [{"number": 2, "decision": "符合"}]})
        second = leads.make_batch(self.db, 20)
        listing = leads.list_batches(self.db)
        self.assertEqual([b["batch_id"] for b in listing["batches"]], [first["batch_id"], second["batch_id"]])
        self.assertEqual(listing["batches"][0]["pending_numbers"], [1])
        self.assertEqual(listing["batches"][0]["reviewed_count"], 1)
        self.assertEqual(listing["open_batches"], [first["batch_id"], second["batch_id"]])
        self.feedback(first)
        self.assertEqual(leads.list_batches(self.db)["open_batches"], [second["batch_id"]])
        self.assertEqual(leads.stats(self.db)["open_batches"], [second["batch_id"]])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(leads.main(["--db", str(self.path), "batch", "--list"]), 0)
        self.assertEqual(json.loads(output.getvalue())["open_batches"], [second["batch_id"]])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 2)

    def test_forget_erases_every_record_and_refuses_silent_reimport(self):
        self.add(candidate("fixture_seed"))
        self.feedback(leads.make_batch(self.db, 20))
        self.add(candidate("fixture_target", sources=[source("fixture_seed")]),
                 candidate("fixture_other", sources=[source("fixture_target", "comment", "https://www.instagram.com/p/FIXTURE/")]))
        batch = leads.make_batch(self.db, 20)
        self.feedback(batch, "不符合", "fixture reason")
        leads.write_progress(self.db, {"entries": [{"username": "fixture_target", "kind": "following", "status": "done", "cursor": "x"}]})
        result = leads.forget(self.db, "@Fixture_Target", "Deletion request from the person")
        self.assertTrue(result["existed"])
        self.assertEqual(result["deleted"]["accounts"], 1)
        self.assertEqual(result["deleted"]["sources_as_seed"], 1)
        for table in ("accounts", "assessments", "evidence", "progress", "progress_events", "batch_items", "reviews"):
            self.assertEqual(self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE username='fixture_target'").fetchone()[0], 0, table)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sources WHERE username='fixture_target' OR source_account='fixture_target'").fetchone()[0], 0)
        self.assertNotIn("fixture_target", json.dumps(leads.batch_view(self.db, batch["batch_id"])))
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertNotIn("fixture_target", json.dumps([tuple(r) for r in self.db.execute("SELECT * FROM forgotten")]))
        with self.assertRaisesRegex(ValueError, "forget"):
            self.add(candidate("fixture_target"))
        self.assertEqual(leads.stats(self.db)["forgotten"], 1)
        self.assertIn("fixture_other", self.views())
        self.assertTrue(leads.forget(self.db, "fixture_target", None, unblock=True)["unblocked"])
        self.add(candidate("fixture_target"))
        self.assertFalse(leads.forget(self.db, "fixture_never_seen", "Nothing stored")["existed"])
        with self.assertRaisesRegex(ValueError, "reason"):
            leads.forget(self.db, "fixture_other", None)

    def test_merge_handles_rename_and_merging_into_existing_account(self):
        self.add(candidate("fixture_seed"))
        self.feedback(leads.make_batch(self.db, 20))
        old = candidate("fixture_old", strong=False, sources=[source("fixture_seed")])
        old["ig_user_id"] = "12345"
        self.add(old, candidate("fixture_child", strong=False, sources=[source("fixture_old", "follower")]))
        batch = leads.make_batch(self.db, 20)
        self.feedback(batch, "符合", "human accepted before rename")
        leads.write_progress(self.db, {"entries": [{"username": "fixture_old", "kind": "posts", "status": "done", "cursor": "all"}]})
        result = leads.merge(self.db, "fixture_old", "fixture_new", "Instagram rename observed")
        self.assertFalse(result["merged_into_existing"])
        views = self.views()
        self.assertNotIn("fixture_old", views)
        self.assertEqual(views["fixture_new"]["status"], "accepted")
        self.assertEqual(views["fixture_new"]["decision_origin"], "human")
        self.assertEqual(views["fixture_new"]["ig_user_id"], "12345")
        self.assertEqual(views["fixture_new"]["profile_url"], "https://www.instagram.com/fixture_new/")
        self.assertEqual(views["fixture_child"]["independent_seeds"], ["fixture_new"])
        self.assertEqual({i["username"] for i in leads.batch_view(self.db, batch["batch_id"])["items"]}, {"fixture_new", "fixture_child"})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM evidence WHERE username='fixture_new'").fetchone()[0], 3)
        self.assertEqual(self.db.execute("SELECT status FROM progress WHERE username='fixture_new' AND kind='posts'").fetchone()[0], "done")
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        # Merge into an account that already exists: relations and evidence are de-duplicated, human decision preserved.
        self.add(candidate("fixture_dup", strong=False, sources=[source("fixture_seed"), source("fixture_seed", "follower")]))
        self.add({"username": "fixture_child", "sources": [source("fixture_dup", "follower")]})
        merged = leads.merge(self.db, "fixture_dup", "fixture_new", "Same person, duplicate record")
        self.assertTrue(merged["merged_into_existing"])
        views = self.views()
        self.assertEqual(views["fixture_new"]["status"], "accepted")
        self.assertEqual({(s["kind"], s["source_account"]) for s in views["fixture_new"]["sources"]}, {("following", "fixture_seed"), ("follower", "fixture_seed")})
        self.assertEqual(len([s for s in views["fixture_child"]["sources"] if s["source_account"] == "fixture_new"]), 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM merge_events").fetchone()[0], 2)
        with self.assertRaisesRegex(ValueError, "不存在"):
            leads.merge(self.db, "fixture_dup", "fixture_new", "again")
        with self.assertRaisesRegex(ValueError, "人工结论不同"):
            self.add(candidate("fixture_rejected", strong=False))
            self.feedback(leads.make_batch(self.db, 20), "不符合")
            leads.merge(self.db, "fixture_rejected", "fixture_new", "conflict")

    def test_ig_user_id_is_validated_and_prevents_duplicate_people(self):
        first = candidate("fixture_one")
        first["ig_user_id"] = 987654321
        self.add(first)
        self.assertEqual(self.views()["fixture_one"]["ig_user_id"], "987654321")
        with self.assertRaisesRegex(ValueError, "merge fixture_one fixture_two"):
            second = candidate("fixture_two")
            second["ig_user_id"] = "987654321"
            self.add(second)
        with self.assertRaisesRegex(ValueError, "不同的 ig_user_id"):
            changed = candidate("fixture_one")
            changed["ig_user_id"] = "1"
            self.add(changed)
        with self.assertRaisesRegex(ValueError, "数字"):
            bad = candidate("fixture_three")
            bad["ig_user_id"] = "abc"
            self.add(bad)
        self.add({"username": "fixture_one", "ig_user_id": "987654321"})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)

    def test_mention_is_a_lead_not_a_seed_relation(self):
        self.add(candidate("fixture_seed"))
        self.feedback(leads.make_batch(self.db, 20))
        self.add(candidate("fixture_mentioned", strong=False, sources=[source("fixture_seed", "mention", "https://www.instagram.com/p/FIXTURE/")]),
                 candidate("fixture_followed", strong=False, sources=[source("fixture_seed")]))
        self.assertEqual(self.views()["fixture_mentioned"]["independent_seed_count"], 0)
        self.assertEqual(self.views()["fixture_followed"]["independent_seed_count"], 1)

    def test_auto_acceptances_are_persisted_and_downgrade_reports_affected_accounts(self):
        for name in ("fixture_seed_one", "fixture_seed_two"):
            self.add(candidate(name))
            self.feedback(leads.make_batch(self.db, 1))
        self.add(candidate("fixture_early_clear"))
        self.assertEqual(self.views()["fixture_early_clear"]["status"], "pending")
        upgrade = leads.set_mode(self.db, "assisted", "Two complete human feedback batches")
        self.assertEqual(upgrade["new_auto_acceptances"], 1)
        self.assertEqual(self.views()["fixture_early_clear"]["status"], "accepted")
        rows = {r[0]: r[1] for r in self.db.execute("SELECT username, trigger FROM auto_acceptances")}
        self.assertEqual(rows, {"fixture_early_clear": "mode_change"})
        result = self.add(candidate("fixture_later_clear"), candidate("fixture_weak", strong=False))
        self.assertEqual(result["new_auto_acceptances"], 1)
        self.assertEqual(self.db.execute("SELECT trigger FROM auto_acceptances WHERE username='fixture_later_clear'").fetchone()[0], "ingest")
        self.assertEqual(leads.stats(self.db)["auto_acceptances"], 2)
        self.assertEqual(leads.stats(self.db)["auto_acceptances_needing_review"], [])
        # A weaker re-assessment flips the status; the earlier automatic acceptance is now flagged for review.
        self.add(candidate("fixture_later_clear", strong=False))
        self.assertEqual(self.views()["fixture_later_clear"]["status"], "pending")
        self.assertTrue(self.views()["fixture_later_clear"]["auto_acceptance_needs_review"])
        self.assertEqual(leads.stats(self.db)["auto_acceptances_needing_review"], ["fixture_later_clear"])
        downgrade = leads.set_mode(self.db, "learning", "Too many corrections")
        self.assertEqual(downgrade["auto_accepted_now_pending"], ["fixture_early_clear", "fixture_later_clear"])
        self.assertEqual(self.views()["fixture_early_clear"]["status"], "pending")
        self.assertTrue(self.views()["fixture_early_clear"]["auto_accepted_before"])
        self.assertFalse(self.views()["fixture_seed_one"]["auto_accepted_before"])

    def test_failed_criteria_are_recorded_and_aggregated(self):
        self.add(candidate("fixture_a"), candidate("fixture_b"), candidate("fixture_c"))
        batch = leads.make_batch(self.db, 20)
        leads.review(self.db, {"batch_id": batch["batch_id"], "reviews": [
            {"number": 1, "decision": "不符合", "reason": "female per bio", "failed_criteria": ["male"]},
            {"number": 2, "decision": "不符合", "failed_criteria": ["resident_hk", "male"]},
            {"number": 3, "decision": "不符合"}]})
        view = leads.batch_view(self.db, batch["batch_id"])
        self.assertEqual(view["items"][1]["reviews"][0]["failed_criteria"], ["male", "resident_hk"])
        counts = leads.stats(self.db)["human_rejections_by_criterion"]
        self.assertEqual(counts, {"personal": 0, "male": 2, "resident_hk": 1, "unspecified": 1})
        with self.assertRaisesRegex(ValueError, "无效项"):
            leads.review(self.db, {"batch_id": batch["batch_id"], "reviews": [{"number": 1, "decision": "不符合", "failed_criteria": ["age"]}]})
        with self.assertRaisesRegex(ValueError, "符合的账号"):
            leads.review(self.db, {"batch_id": batch["batch_id"], "reviews": [{"number": 1, "decision": "符合", "failed_criteria": ["male"]}]})
        # Same decision with different failed_criteria is a new record, identical one is ignored.
        leads.review(self.db, {"batch_id": batch["batch_id"], "reviews": [{"number": 3, "decision": "不符合", "failed_criteria": ["personal"]}]})
        leads.review(self.db, {"batch_id": batch["batch_id"], "reviews": [{"number": 3, "decision": "不符合", "failed_criteria": ["personal"]}]})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reviews WHERE number=3").fetchone()[0], 2)

    def test_stats_report_why_pending_accounts_fail_the_automatic_gate(self):
        self.add(candidate("fixture_unknown_male"), candidate("fixture_weak", residence_signal="flag"))
        self.add({"username": "fixture_unknown_male", "assessment": {**candidate("fixture_unknown_male")["assessment"], "male": "unknown"},
                  "evidence": candidate("fixture_unknown_male")["evidence"]})
        blockers = leads.stats(self.db)["pending_gate_blockers"]
        self.assertEqual(blockers, {"male:unknown": 1, "resident_hk:weak_signal": 1})

    def test_usernames_reject_reserved_instagram_paths(self):
        for bad in ("https://www.instagram.com/direct/inbox/", "https://www.instagram.com/explore/tags/hk/",
                    "https://www.instagram.com/locations/1/", "https://www.instagram.com/accounts/login/", "explore", "reels"):
            with self.assertRaises(ValueError, msg=bad):
                leads.username(bad)
        self.assertEqual(leads.username("https://m.instagram.com/Some.User_1/?hl=zh"), "some.user_1")

    def test_v1_database_is_migrated_with_data_preserved(self):
        v1_schema = """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE accounts (username TEXT PRIMARY KEY, profile_url TEXT NOT NULL, assessment_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
            assessment_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, manual_decision TEXT, manual_reason TEXT, manual_at TEXT);
        CREATE TABLE assessments (id INTEGER PRIMARY KEY, username TEXT NOT NULL REFERENCES accounts(username), assessment_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL, assessment_hash TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE evidence (evidence_id TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES accounts(username), criterion TEXT NOT NULL,
            signal TEXT NOT NULL, text TEXT NOT NULL, url TEXT NOT NULL, observed_at TEXT NOT NULL);
        CREATE TABLE sources (source_id TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES accounts(username), kind TEXT NOT NULL,
            source_account TEXT NOT NULL, source_url TEXT NOT NULL, source_key TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
        CREATE TABLE batches (batch_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, mode TEXT NOT NULL);
        CREATE TABLE batch_items (batch_id TEXT NOT NULL REFERENCES batches(batch_id), number INTEGER NOT NULL,
            username TEXT NOT NULL REFERENCES accounts(username), snapshot_json TEXT NOT NULL, assessment_hash TEXT NOT NULL,
            PRIMARY KEY(batch_id, number), UNIQUE(batch_id, username));
        CREATE TABLE reviews (id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, number INTEGER NOT NULL,
            username TEXT NOT NULL REFERENCES accounts(username), decision TEXT NOT NULL, reason TEXT NOT NULL,
            origin TEXT NOT NULL CHECK(origin = 'human_feedback'), created_at TEXT NOT NULL,
            FOREIGN KEY(batch_id, number) REFERENCES batch_items(batch_id, number));
        CREATE TABLE rules (version INTEGER PRIMARY KEY, based_on_batches_json TEXT NOT NULL, rules_json TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE mode_events (id INTEGER PRIMARY KEY, old_mode TEXT NOT NULL, new_mode TEXT NOT NULL, reason TEXT NOT NULL, completed_batches_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE progress (username TEXT NOT NULL REFERENCES accounts(username), kind TEXT NOT NULL, source_key TEXT NOT NULL, status TEXT NOT NULL,
            cursor_json TEXT NOT NULL, reason TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(username, kind, source_key));
        CREATE TABLE progress_events (id INTEGER PRIMARY KEY, username TEXT NOT NULL REFERENCES accounts(username), kind TEXT NOT NULL,
            source_key TEXT NOT NULL, status TEXT NOT NULL, cursor_json TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE discovery_progress (kind TEXT NOT NULL, source_key TEXT NOT NULL, status TEXT NOT NULL, cursor_json TEXT NOT NULL,
            reason TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(kind, source_key));
        CREATE TABLE discovery_progress_events (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, source_key TEXT NOT NULL, status TEXT NOT NULL,
            cursor_json TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version','1'),('tool','instagram-hk-leads'),('mode','learning');
        """
        legacy = self.base / "legacy.sqlite"
        assessment = json.dumps({"personal": "yes", "male": "yes", "resident_hk": "yes", "confidence": "clear", "reason": "fixture"}, sort_keys=True)
        with contextlib.closing(sqlite3.connect(legacy)) as db:
            with db:
                db.executescript(v1_schema)
                db.execute("INSERT INTO accounts VALUES ('fixture_legacy','https://www.instagram.com/fixture_legacy/',?, '[]','h','t','t',NULL,NULL,NULL)", (assessment,))
                db.execute("INSERT INTO batches VALUES ('batch_legacy','t','learning')")
                db.execute("INSERT INTO batch_items VALUES ('batch_legacy',1,'fixture_legacy','{\"model_eligible\": false}','h')")
                db.execute("INSERT INTO reviews (batch_id,number,username,decision,reason,origin,created_at) VALUES ('batch_legacy',1,'fixture_legacy','accepted','kept','human_feedback','t')")
        with self.assertRaisesRegex(ValueError, "旧版本 1"):
            leads.connect(legacy)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(leads.main(["--db", str(legacy), "stats"]), 2)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(leads.main(["--db", str(legacy), "migrate"]), 0)
        self.assertEqual(json.loads(output.getvalue())["migrations_applied"], ["1->2", "2->3"])
        with contextlib.closing(leads.connect(legacy)) as db:
            self.assertEqual(leads.schema_version(db), leads.CURRENT_SCHEMA)
            self.assertEqual(db.migrations_applied, [])
            views = {item["username"]: item for item in leads.account_views(db)}
            self.assertEqual(views["fixture_legacy"]["ig_user_id"], None)
            self.assertEqual(leads.batch_view(db, "batch_legacy")["items"][0]["reviews"][0]["failed_criteria"], [])
            self.assertEqual(leads.stats(db)["completed_human_batches"], ["batch_legacy"])
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            leads.ingest(db, {"accounts": [{**candidate("fixture_after"), "ig_user_id": "42"}]})
            leads.forget(db, "fixture_legacy", "fixture erasure")
            db.commit()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM forgotten").fetchone()[0], 1)
        # A database claiming a newer schema than this tool knows is refused, not downgraded.
        with contextlib.closing(sqlite3.connect(legacy)) as db:
            with db:
                db.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        with self.assertRaisesRegex(ValueError, "高于"):
            leads.connect(legacy)


if __name__ == "__main__":
    unittest.main()
