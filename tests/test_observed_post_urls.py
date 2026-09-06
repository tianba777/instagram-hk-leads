"""Synthetic coverage for post links with an owner prefix seen in the browser."""

import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "leads.py"
SPEC = importlib.util.spec_from_file_location("observed_post_url_leads", SCRIPT)
leads = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(leads)
AT = "2026-09-05T09:00:00+08:00"


def legacy_url(value, label="url", optional=False):
    """The previous url() behavior, retained here to create pre-fix synthetic rows."""
    if optional and (value is None or value == ""):
        return ""
    parsed = urlsplit(leads.nonempty(value, label))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        leads.fail(f"{label} 必须是无登录凭据的 http/https URL")
    if parsed.hostname.lower() in leads.INSTAGRAM_HOSTS:
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                 if key.lower() not in {"igsh", "igshid", "fbclid", "gclid"} and not key.lower().startswith("utm_")]
        return urlunsplit(("https", "www.instagram.com", parsed.path.rstrip("/") + "/", urlencode(sorted(query)), ""))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


class ObservedPostUrlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = leads.connect(Path(self.temp.name) / "synthetic.sqlite", initialize=True)
        leads.ingest(self.db, {"accounts": [{"username": "fixture_candidate"}]})

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def progress(self, link, **changes):
        entry = {"username": "fixture_candidate", "kind": "comment", "source_key": link,
                 "status": "in_progress", "cursor": {"last_visible_comment": "synthetic"}}
        entry.update(changes)
        return leads.write_progress(self.db, {"entries": [entry]})

    def legacy_account(self, name="fixture_legacy"):
        link = "https://www.instagram.com/fixture_owner/p/Fixture_A-1/?comment_id=123&igsh=tracking"
        account = {
            "username": name,
            "assessment": {"personal": "yes", "male": "yes", "resident_hk": "yes",
                           "confidence": "clear", "reason": "Synthetic previous-version assessment"},
            "evidence": [{"criterion": criterion, "signal": "self_description", "text": "Synthetic evidence only",
                          "url": link, "observed_at": AT} for criterion in leads.CRITERIA],
            "sources": [{"kind": "comment", "source_account": "fixture_owner", "source_url": link,
                         "observed_at": AT}],
        }
        with patch.object(leads, "url", legacy_url):
            leads.ingest(self.db, {"accounts": [account]})
        self.assertIn("/fixture_owner/p/", self.db.execute("SELECT url FROM evidence WHERE username=?", (name,)).fetchone()[0])
        return account

    def stored_rows(self, table, name="fixture_legacy"):
        return [dict(row) for row in self.db.execute(f"SELECT * FROM {table} WHERE username=?", (name,))]

    def test_public_url_unifies_owner_and_root_links_and_preserves_business_query(self):
        for kind in ("p", "reel", "tv"):
            expected = f"https://www.instagram.com/{kind}/Fixture_A-1/?comment_id=123"
            for owner in ("", "Fixture.Owner_1/"):
                with self.subTest(kind=kind, owner=owner):
                    actual = leads.url(
                        f"http://m.instagram.com/{owner}{kind}/Fixture_A-1"
                        "?utm_source=fixture&igsh=tracking&comment_id=123&fbclid=tracking#comments")
                    self.assertEqual(actual, expected)

    def test_source_deduplication_uses_canonical_post_key_with_or_without_explicit_key(self):
        for kind, explicit_key in (("comment", False), ("reply", True)):
            owner_link = "https://instagram.com/fixture_owner/reel/Fixture_A-1/?comment_id=123&igsh=first"
            root_link = "https://www.instagram.com/reel/Fixture_A-1/?utm_campaign=second&comment_id=123"
            for link in (owner_link, root_link):
                source = {"kind": kind, "source_account": "fixture_owner", "source_url": link,
                          "observed_at": AT}
                if explicit_key:
                    source["source_key"] = link
                leads.ingest(self.db, {"accounts": [{"username": "fixture_candidate", "sources": [source]}]})
            rows = self.db.execute("SELECT source_key,source_url FROM sources WHERE kind=?", (kind,)).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(tuple(rows[0]), ("https://www.instagram.com/reel/Fixture_A-1/?comment_id=123",) * 2)

    def test_comment_progress_is_shared_between_owner_and_root_links(self):
        for kind in ("p", "reel", "tv"):
            owner_link = f"https://instagram.com/fixture_owner/{kind}/Fixture_A-1/?igsh=tracking"
            root_link = f"https://www.instagram.com/{kind}/Fixture_A-1/"
            self.progress(owner_link)
            self.progress(root_link)
            rows = self.db.execute("SELECT source_key,status FROM progress WHERE source_key=?", (root_link,)).fetchall()
            self.assertEqual([tuple(row) for row in rows], [(root_link, "in_progress")])
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM progress_events WHERE source_key=?", (root_link,)).fetchone()[0], 1)
            self.progress(root_link, status="done")
            self.assertEqual(self.db.execute("SELECT status FROM progress WHERE source_key=?", (root_link,)).fetchone()[0], "done")

    def test_distinct_comment_ids_are_preserved_in_progress_and_sources(self):
        for comment_id in ("123", "456"):
            link = f"https://www.instagram.com/fixture_owner/p/Fixture_A-1/?comment_id={comment_id}"
            self.progress(link)
            source = {"kind": "comment", "source_account": "fixture_owner", "source_url": link,
                      "observed_at": AT}
            leads.ingest(self.db, {"accounts": [{"username": "fixture_candidate", "sources": [source]}]})
        expected = {f"https://www.instagram.com/p/Fixture_A-1/?comment_id={value}" for value in ("123", "456")}
        for table in ("progress", "sources"):
            self.assertEqual({row[0] for row in self.db.execute(f"SELECT source_key FROM {table}")}, expected)

    def test_comment_progress_rejects_non_posts_and_invalid_owner_prefixes(self):
        paths = (
            "/fixture_owner/", "/fixture_owner/reels/", "/fixture_owner/p/", "/p/",
            "/fixture_owner/p/Fixture_A-1/extra/", "/fixture_owner/notpost/Fixture_A-1/",
            "/p/Fixture_A-1//", "/fixture_owner//p/Fixture_A-1/", "/p/invalid%2Fcode/",
            "/invalid-owner/p/Fixture_A-1/", "/@fixture_owner/p/Fixture_A-1/",
            "/../p/Fixture_A-1/", "/./p/Fixture_A-1/", "/" + "a" * 31 + "/p/Fixture_A-1/",
        )
        links = ["https://www.instagram.com" + path for path in paths]
        links += [f"https://www.instagram.com/{owner}/p/Fixture_A-1/" for owner in leads.RESERVED_PATHS]
        links += ["https://example.com/fixture_owner/p/Fixture_A-1/"]
        for link in links:
            with self.subTest(link=link), self.assertRaisesRegex(ValueError, "具体 Instagram"):
                self.progress(link)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM progress").fetchone()[0], 0)

    def test_legacy_full_reimport_preserves_assessments_and_completed_review(self):
        account = self.legacy_account()
        batch = leads.make_batch(self.db, 20)
        leads.review(self.db, {"batch_id": batch["batch_id"], "reviews": [
            {"number": entry["number"], "decision": "不确定", "reason": "Synthetic human feedback"}
            for entry in batch["items"]]})
        tables = ("accounts", "assessments", "evidence", "sources", "batch_items", "reviews")
        before = {table: self.stored_rows(table) for table in tables}
        for use_root_link in (False, True):
            repeated = copy.deepcopy(account)
            if use_root_link:
                for entry in repeated["evidence"]:
                    entry["url"] = leads.url(entry["url"])
                repeated["sources"][0]["source_url"] = leads.url(repeated["sources"][0]["source_url"])
            result = leads.ingest(self.db, {"accounts": [repeated]})
            for counter in ("updated_assessments", "new_sources", "new_evidence", "new_auto_acceptances"):
                self.assertEqual(result[counter], 0, counter)
            self.assertEqual({table: self.stored_rows(table) for table in tables}, before)
            self.assertIsNone(leads.make_batch(self.db, 20)["batch_id"])

    def test_legacy_evidence_only_reimport_preserves_existing_rows(self):
        account = self.legacy_account()
        before = {table: self.stored_rows(table) for table in ("accounts", "assessments", "evidence")}
        repeated = {"username": account["username"], "evidence": account["evidence"]}
        result = leads.ingest(self.db, {"accounts": [repeated]})
        self.assertEqual(result["new_evidence"], 0)
        self.assertEqual(result["updated_assessments"], 0)
        self.assertEqual({table: self.stored_rows(table) for table in before}, before)

    def test_legacy_source_equivalence_extends_original_seen_range(self):
        account = self.legacy_account()
        original = self.stored_rows("sources")[0]
        for seen in ("2026-09-04T01:00:00Z", "2026-09-06T01:00:00Z"):
            source = copy.deepcopy(account["sources"][0])
            source["source_url"] = leads.url(source["source_url"])
            source["source_key"] = source["source_url"]
            source["observed_at"] = seen
            result = leads.ingest(self.db, {"accounts": [{"username": account["username"], "sources": [source]}]})
            self.assertEqual(result["new_sources"], 0)
        rows = self.stored_rows("sources")
        self.assertEqual(len(rows), 1)
        expected = {**original, "first_seen": "2026-09-04T01:00:00+00:00", "last_seen": "2026-09-06T01:00:00+00:00"}
        self.assertEqual(rows[0], expected)

    def test_legacy_equivalence_does_not_hide_substantive_changes(self):
        for change in ("text", "observation_time", "comment_id", "assessment"):
            with self.subTest(change=change):
                account = self.legacy_account("fixture_" + change)
                if change == "text":
                    account["evidence"][0]["text"] = "Additional synthetic evidence"
                elif change == "observation_time":
                    account["evidence"][0]["observed_at"] = "2026-09-06T01:00:00Z"
                elif change == "comment_id":
                    account["evidence"][0]["url"] = account["evidence"][0]["url"].replace("comment_id=123", "comment_id=456")
                    account["sources"][0]["source_url"] = account["sources"][0]["source_url"].replace("comment_id=123", "comment_id=456")
                else:
                    account["assessment"]["reason"] = "Revised synthetic assessment"
                result = leads.ingest(self.db, {"accounts": [account]})
                self.assertEqual(result["updated_assessments"], 1)
                self.assertEqual(result["new_evidence"], 0 if change == "assessment" else 1)
                self.assertEqual(result["new_sources"], 1 if change == "comment_id" else 0)


if __name__ == "__main__":
    unittest.main()
