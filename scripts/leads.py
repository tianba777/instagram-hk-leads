#!/usr/bin/env python3
"""Local Instagram candidate records. Standard library only; never accesses Instagram."""

import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CRITERIA = ("personal", "male", "resident_hk")
MODES = ("learning", "assisted", "automatic")
SOURCE_KINDS = {"search", "place", "following", "follower", "comment", "reply", "mention"}
PROGRESS_KINDS = {"following", "follower", "posts", "comment", "search", "place"}
PROGRESS_STATES = {"pending", "in_progress", "done", "unavailable"}
STRONG_RESIDENCE_SIGNALS = {"explicit_residence", "recent_hk_life", "self_description", "recurring_local_life"}
DECISIONS = {
    "符合": "accepted", "不符合": "rejected", "不确定": "uncertain",
    "accepted": "accepted", "rejected": "rejected", "uncertain": "uncertain",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
 username TEXT PRIMARY KEY, profile_url TEXT NOT NULL,
 assessment_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
 assessment_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 manual_decision TEXT, manual_reason TEXT, manual_at TEXT
);
CREATE TABLE IF NOT EXISTS assessments (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL REFERENCES accounts(username),
 assessment_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
 assessment_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
 evidence_id TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES accounts(username),
 criterion TEXT NOT NULL, signal TEXT NOT NULL, text TEXT NOT NULL,
 url TEXT NOT NULL, observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
 source_id TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES accounts(username),
 kind TEXT NOT NULL, source_account TEXT NOT NULL, source_url TEXT NOT NULL,
 source_key TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sources_target ON sources(username);
CREATE TABLE IF NOT EXISTS batches (
 batch_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, mode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_items (
 batch_id TEXT NOT NULL REFERENCES batches(batch_id), number INTEGER NOT NULL,
 username TEXT NOT NULL REFERENCES accounts(username), snapshot_json TEXT NOT NULL,
 assessment_hash TEXT NOT NULL, PRIMARY KEY(batch_id, number), UNIQUE(batch_id, username)
);
CREATE TABLE IF NOT EXISTS reviews (
 id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, number INTEGER NOT NULL,
 username TEXT NOT NULL REFERENCES accounts(username), decision TEXT NOT NULL,
 reason TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin = 'human_feedback'),
 created_at TEXT NOT NULL,
 FOREIGN KEY(batch_id, number) REFERENCES batch_items(batch_id, number)
);
CREATE TABLE IF NOT EXISTS rules (
 version INTEGER PRIMARY KEY, based_on_batches_json TEXT NOT NULL,
 rules_json TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mode_events (
 id INTEGER PRIMARY KEY, old_mode TEXT NOT NULL, new_mode TEXT NOT NULL,
 reason TEXT NOT NULL, completed_batches_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS progress (
 username TEXT NOT NULL REFERENCES accounts(username), kind TEXT NOT NULL,
 source_key TEXT NOT NULL, status TEXT NOT NULL, cursor_json TEXT NOT NULL,
 reason TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(username, kind, source_key)
);
CREATE TABLE IF NOT EXISTS progress_events (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL REFERENCES accounts(username),
 kind TEXT NOT NULL, source_key TEXT NOT NULL, status TEXT NOT NULL,
 cursor_json TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_progress (
 kind TEXT NOT NULL, source_key TEXT NOT NULL, status TEXT NOT NULL,
 cursor_json TEXT NOT NULL, reason TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(kind, source_key)
);
CREATE TABLE IF NOT EXISTS discovery_progress_events (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, source_key TEXT NOT NULL,
 status TEXT NOT NULL, cursor_json TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(dump(value).encode("utf-8")).hexdigest()


def fail(message):
    raise ValueError(message)


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        fail(f"{label} 必须是非空文本")
    return value.strip()


def timestamp(value, label):
    text = nonempty(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        fail(f"{label} 必须是带时区的 ISO 8601 时间")
    if parsed.tzinfo is None:
        fail(f"{label} 必须包含时区")
    return parsed.astimezone(timezone.utc).isoformat()


def url(value, label="url", optional=False):
    if optional and (value is None or value == ""):
        return ""
    text = nonempty(value, label)
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        fail(f"{label} 必须是无登录凭据的 http/https URL")
    if parsed.hostname.lower() in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                 if key.lower() not in {"igsh", "igshid", "fbclid", "gclid"} and not key.lower().startswith("utm_")]
        return urlunsplit(("https", "www.instagram.com", parsed.path.rstrip("/") + "/", urlencode(sorted(query)), ""))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def username(value):
    text = nonempty(value, "username")
    if "://" in text:
        parsed = urlsplit(text)
        if parsed.hostname not in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
            fail("主页 URL 必须属于 instagram.com")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1 or parts[0].lower() in {"p", "reel", "reels", "explore", "accounts", "stories"}:
            fail("需要账号主页 URL，不能使用帖子或其他页面 URL")
        text = parts[0]
    text = text.removeprefix("@").lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", text):
        fail("username 只能包含 1–30 个英文字母、数字、点或下划线")
    return text


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        fail("JSON 顶层必须是对象")
    return data


def records(data, key):
    values = data.get(key)
    if not isinstance(values, list) or not values:
        fail(f"{key} 必须是非空列表")
    if not all(isinstance(item, dict) for item in values):
        fail(f"{key} 中每一项必须是对象")
    return values


def connect(path, initialize=False):
    target = Path(path).expanduser()
    if not initialize and not target.is_file():
        fail("数据库不存在；请使用显式 --db 路径运行 init")
    existing_nonempty = target.is_file() and target.stat().st_size > 0
    if initialize and not existing_nonempty:
        target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(target)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    # Verify ownership before any schema/metadata write. Never initialize over another database.
    if existing_nonempty or not initialize:
        try:
            metadata = dict(db.execute("SELECT key,value FROM meta"))
            if metadata.get("tool") != "instagram-hk-leads" or metadata.get("schema_version") != "1":
                fail("该文件不是本工具支持的 leads 数据库；未修改文件")
        except (sqlite3.Error, ValueError):
            db.close()
            fail("该文件不是本工具支持的 leads 数据库；未修改文件")
    if initialize:
        db.executescript(SCHEMA)
        db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', '1')")
        db.execute("INSERT OR IGNORE INTO meta VALUES ('tool', 'instagram-hk-leads')")
        db.execute("INSERT OR IGNORE INTO meta VALUES ('mode', 'learning')")
        db.commit()
    try:
        version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        fail("该文件不是已初始化的 leads 数据库")
    if not version or version[0] != "1":
        fail("不支持此数据库版本")
    return db


def current_mode(db):
    return db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()[0]


def normalize_assessment(value):
    if not isinstance(value, dict):
        fail("assessment 必须是对象")
    result = {}
    for criterion in CRITERIA:
        choice = value.get(criterion, "unknown")
        if choice not in {"yes", "no", "unknown"}:
            fail(f"assessment.{criterion} 必须是 yes/no/unknown")
        result[criterion] = choice
    result["confidence"] = value.get("confidence", "uncertain")
    if result["confidence"] not in {"clear", "uncertain"}:
        fail("confidence 必须是 clear/uncertain")
    result["reason"] = value.get("reason", "")
    if not isinstance(result["reason"], str):
        fail("assessment.reason 必须是文本")
    if "rule_version" in value:
        if type(value["rule_version"]) is not int or value["rule_version"] < 0:
            fail("assessment.rule_version 必须是非负整数")
        result["rule_version"] = value["rule_version"]
    return result


def normalize_evidence(values):
    if not isinstance(values, list):
        fail("evidence 必须是列表")
    result = []
    for item in values:
        if not isinstance(item, dict):
            fail("evidence 每一项必须是对象")
        entry = {
            "criterion": nonempty(item.get("criterion"), "evidence.criterion"),
            "signal": nonempty(item.get("signal"), "evidence.signal").lower(),
            "text": item.get("text", ""),
            "url": url(item.get("url"), "evidence.url", optional=True),
            "observed_at": timestamp(item["observed_at"], "evidence.observed_at") if item.get("observed_at") else "",
        }
        if not isinstance(entry["text"], str):
            fail("evidence.text 必须是文本")
        if entry not in result:
            result.append(entry)
    return sorted(result, key=dump)


def model_gate(assessment, evidence):
    reasons = []
    for criterion in CRITERIA:
        if assessment[criterion] != "yes":
            reasons.append(f"{criterion} 不是 yes")
    if assessment["confidence"] != "clear":
        reasons.append("confidence 不是 clear")
    if not assessment["reason"].strip():
        reasons.append("缺少判断理由")
    for criterion in CRITERIA:
        matching = [e for e in evidence if e["criterion"] == criterion and e["text"].strip() and e["url"] and e["observed_at"]]
        if not matching:
            reasons.append(f"{criterion} 缺少可追溯证据")
        elif criterion == "resident_hk" and not any(e["signal"] in STRONG_RESIDENCE_SIGNALS for e in matching):
            reasons.append("缺少明确现居自述或近期持续香港生活证据；未知 signal 默认不能自动通过")
    return not reasons, reasons


def status(row, mode):
    if row["manual_decision"]:
        decision = row["manual_decision"]
        return ("pending" if decision == "uncertain" else decision), "human"
    passed, _ = model_gate(json.loads(row["assessment_json"]), json.loads(row["evidence_json"]))
    if mode != "learning" and passed:
        return "accepted", "automatic"
    return "pending", "model_pending"


def account_views(db):
    mode = current_mode(db)
    rows = db.execute("SELECT * FROM accounts").fetchall()
    states = {r["username"]: status(r, mode) for r in rows}
    all_sources = {}
    for source in db.execute("SELECT * FROM sources ORDER BY first_seen, source_id"):
        all_sources.setdefault(source["username"], []).append(dict(source))
    result = []
    for row in rows:
        item = dict(row)
        item["assessment"] = json.loads(item.pop("assessment_json"))
        item["evidence"] = json.loads(item.pop("evidence_json"))
        item["status"], item["decision_origin"] = states[row["username"]]
        item["model_eligible"], item["model_gate_reasons"] = model_gate(item["assessment"], item["evidence"])
        item["sources"] = all_sources.get(row["username"], [])
        seeds = sorted({s["source_account"] for s in item["sources"]
                        if s["kind"] in {"following", "follower", "comment", "reply", "mention"}
                        and s["source_account"] != row["username"]
                        and states.get(s["source_account"], (None,))[0] == "accepted"})
        item["independent_seed_count"] = len(seeds)
        item["independent_seeds"] = seeds
        result.append(item)
    return sorted(result, key=lambda item: (-item["independent_seed_count"], item["created_at"], item["username"]))


def ingest(db, data):
    inserted = updated = source_count = evidence_count = 0
    for incoming in records(data, "accounts"):
        name = username(incoming.get("username") or incoming.get("profile_url"))
        if incoming.get("profile_url") and username(incoming["profile_url"]) != name:
            fail("username 与 profile_url 不一致")
        existing = db.execute("SELECT * FROM accounts WHERE username=?", (name,)).fetchone()
        has_assessment = "assessment" in incoming
        assessment = normalize_assessment(incoming["assessment"]) if has_assessment else (
            json.loads(existing["assessment_json"]) if existing else normalize_assessment({}))
        incoming_evidence = normalize_evidence(incoming.get("evidence", []))
        # New assessments must carry their own evidence; old observations cannot silently prove a new judgment.
        evidence = incoming_evidence if has_assessment or not existing else json.loads(existing["evidence_json"])
        assessment_hash = digest([assessment, evidence])
        changed = not existing or existing["assessment_hash"] != assessment_hash
        observed = now()
        if not existing:
            db.execute("INSERT INTO accounts (username,profile_url,assessment_json,evidence_json,assessment_hash,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                       (name, f"https://www.instagram.com/{name}/", dump(assessment), dump(evidence), assessment_hash, observed, observed))
            inserted += 1
        elif changed:
            db.execute("UPDATE accounts SET assessment_json=?,evidence_json=?,assessment_hash=?,updated_at=? WHERE username=?",
                       (dump(assessment), dump(evidence), assessment_hash, observed, name))
            updated += 1
        if changed:
            db.execute("INSERT INTO assessments (username,assessment_json,evidence_json,assessment_hash,created_at) VALUES (?,?,?,?,?)",
                       (name, dump(assessment), dump(evidence), assessment_hash, observed))
        for entry in incoming_evidence:
            evidence_count += db.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?,?)",
                (digest([name, entry]), name, entry["criterion"], entry["signal"], entry["text"], entry["url"], entry["observed_at"])).rowcount
        values = incoming.get("sources", [])
        if not isinstance(values, list):
            fail("sources 必须是列表")
        for source in values:
            if not isinstance(source, dict) or source.get("kind") not in SOURCE_KINDS:
                fail("sources.kind 不受支持")
            kind = source["kind"]
            origin = username(source["source_account"]) if source.get("source_account") else ""
            if kind in {"following", "follower", "comment", "reply", "mention"} and not origin:
                fail(f"{kind} 来源必须提供 source_account")
            source_url = url(source.get("source_url"), "sources.source_url")
            key = kind if kind in {"following", "follower"} else nonempty(source.get("source_key") or source_url, "sources.source_key")
            if "://" in key:
                key = url(key, "sources.source_key")
            seen = timestamp(source.get("observed_at"), "sources.observed_at")
            source_id = digest([name, kind, origin, key])
            previous = db.execute("SELECT first_seen,last_seen FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if previous:
                db.execute("UPDATE sources SET first_seen=?,last_seen=? WHERE source_id=?",
                           (min(previous[0], seen), max(previous[1], seen), source_id))
            else:
                db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)", (source_id, name, kind, origin, source_url, key, seen, seen))
                source_count += 1
    return {"inserted_accounts": inserted, "updated_assessments": updated, "new_sources": source_count,
            "new_evidence": evidence_count, "mode": current_mode(db)}


def batch_view(db, batch_id):
    batch = db.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if not batch:
        fail("batch_id 不存在")
    items = []
    for row in db.execute("SELECT * FROM batch_items WHERE batch_id=? ORDER BY number", (batch_id,)):
        entry = {"number": row["number"], "username": row["username"], "snapshot": json.loads(row["snapshot_json"])}
        entry["reviews"] = [dict(r) for r in db.execute("SELECT id,decision,reason,origin,created_at FROM reviews WHERE batch_id=? AND number=? ORDER BY id", (batch_id, row["number"]))]
        items.append(entry)
    return {**dict(batch), "items": items}


def make_batch(db, limit, include_reviewed=False):
    open_accounts = {r[0] for r in db.execute("SELECT i.username FROM batch_items i WHERE NOT EXISTS (SELECT 1 FROM reviews r WHERE r.batch_id=i.batch_id AND r.number=i.number)")}
    reviewed_hashes = {(r[0], r[1]) for r in db.execute("SELECT DISTINCT i.username,i.assessment_hash FROM batch_items i JOIN reviews r ON r.batch_id=i.batch_id AND r.number=i.number")}
    candidates = [a for a in account_views(db) if a["username"] not in open_accounts
                  and (include_reviewed or a["status"] == "pending")
                  and (include_reviewed or (not a["manual_decision"] and (a["username"], a["assessment_hash"]) not in reviewed_hashes)
                       or (a["manual_decision"] == "uncertain" and (a["username"], a["assessment_hash"]) not in reviewed_hashes))]
    selected = candidates[:limit]
    if not selected:
        return {"batch_id": None, "items": [], "reason": "没有新的待审账号；既有批次和历史判断保持不变"}
    batch_id = "batch_" + uuid.uuid4().hex[:16]
    db.execute("INSERT INTO batches VALUES (?,?,?)", (batch_id, now(), current_mode(db)))
    for number, account in enumerate(selected, start=1):
        db.execute("INSERT INTO batch_items VALUES (?,?,?,?,?)", (batch_id, number, account["username"], dump(account), account["assessment_hash"]))
    return batch_view(db, batch_id)


def review(db, data):
    batch_id = nonempty(data.get("batch_id"), "batch_id")
    entries = records(data, "reviews")
    numbers = [entry.get("number") for entry in entries]
    if any(type(number) is not int or number < 1 for number in numbers) or len(set(numbers)) != len(numbers):
        fail("同一份反馈中的 number 必须是互不重复的正整数")
    changed = 0
    for entry in entries:
        item = db.execute("SELECT * FROM batch_items WHERE batch_id=? AND number=?", (batch_id, entry["number"])).fetchone()
        if not item:
            fail("找不到 batch_id + number 对应的账号")
        if entry.get("decision") not in DECISIONS:
            fail("decision 必须是 符合/不符合/不确定 或 accepted/rejected/uncertain")
        decision = DECISIONS[entry["decision"]]
        reason = entry.get("reason", "")
        if not isinstance(reason, str):
            fail("review.reason 必须是文本；未提供时保留为空，不编造理由")
        previous = db.execute("SELECT decision,reason FROM reviews WHERE batch_id=? AND number=? ORDER BY id DESC LIMIT 1", (batch_id, entry["number"])).fetchone()
        if previous and tuple(previous) == (decision, reason):
            continue
        at = now()
        db.execute("INSERT INTO reviews (batch_id,number,username,decision,reason,origin,created_at) VALUES (?,?,?,?,?,'human_feedback',?)",
                   (batch_id, entry["number"], item["username"], decision, reason, at))
        db.execute("UPDATE accounts SET manual_decision=?,manual_reason=?,manual_at=? WHERE username=?", (decision, reason, at, item["username"]))
        changed += 1
    return {"recorded_reviews": changed, "batch": batch_view(db, batch_id)}


def completed_batches(db):
    return [r[0] for r in db.execute("SELECT b.batch_id FROM batches b WHERE EXISTS (SELECT 1 FROM batch_items i WHERE i.batch_id=b.batch_id) AND NOT EXISTS (SELECT 1 FROM batch_items i WHERE i.batch_id=b.batch_id AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.batch_id=i.batch_id AND r.number=i.number AND r.origin='human_feedback')) ORDER BY b.created_at")]


def set_mode(db, desired, reason):
    previous = current_mode(db)
    complete = completed_batches(db)
    if desired is None or desired == previous:
        return {"mode": previous, "completed_human_batches": complete, "minimum_for_increase": 2}
    reason = nonempty(reason, "mode --reason")
    if MODES.index(desired) > MODES.index(previous) and len(complete) < 2:
        fail("提高自动程度需要至少 2 个完整的人工审核批次；模型判断或自动入选不计入")
    db.execute("UPDATE meta SET value=? WHERE key='mode'", (desired,))
    db.execute("INSERT INTO mode_events (old_mode,new_mode,reason,completed_batches_json,created_at) VALUES (?,?,?,?,?)",
               (previous, desired, reason, dump(complete), now()))
    return {"previous_mode": previous, "mode": desired, "reason": reason, "completed_human_batches": complete,
            "note": "仅更改审核方式和记录依据；没有训练模型。未知或证据不足的账号仍待定。"}


def learn(db, data):
    reason = nonempty(data.get("reason"), "learn.reason")
    rules = data.get("rules")
    batches = data.get("based_on_batches")
    if not isinstance(rules, list) or not rules or not all(isinstance(item, str) and item.strip() for item in rules):
        fail("rules 必须是非空规则文本列表")
    if not isinstance(batches, list) or not batches or not all(isinstance(item, str) for item in batches):
        fail("based_on_batches 必须是非空批次 ID 列表")
    reviewed = {row[0] for row in db.execute("SELECT DISTINCT batch_id FROM reviews WHERE origin='human_feedback'")}
    if any(batch not in reviewed for batch in batches):
        fail("规则依据只能引用至少已有一条真实人工反馈的批次；空反馈和自动判断不能作为人工反馈")
    version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM rules").fetchone()[0]
    db.execute("INSERT INTO rules VALUES (?,?,?,?,?)", (version, dump(sorted(set(batches))), dump(rules), reason, now()))
    return {"version": version, "based_on_batches": sorted(set(batches)), "rules": rules, "reason": reason,
            "note": "仅总结版本创建前已经收到的真实人工反馈，未审条目不作为依据；由 agent 阅读并应用规则，工具未训练模型。"}


def write_progress(db, data):
    for entry in records(data, "entries"):
        kind = entry.get("kind")
        state = entry.get("status")
        if kind not in PROGRESS_KINDS or state not in PROGRESS_STATES:
            fail("progress.kind 或 status 不受支持")
        discovery = kind in {"search", "place"}
        name = None if discovery else username(entry.get("username"))
        if not discovery and not db.execute("SELECT 1 FROM accounts WHERE username=?", (name,)).fetchone():
            fail(f"进度账号尚未导入：{name}")
        key = nonempty(entry.get("source_key"), "discovery.source_key") if discovery else kind
        if kind == "comment":
            key = url(entry.get("source_key"), "comment.source_key")
        elif discovery and "://" in key:
            key = url(key, "discovery.source_key")
        if kind == "comment":
            parsed = urlsplit(key)
            if parsed.hostname != "www.instagram.com" or not re.fullmatch(r"/(?:p|reel|tv)/[^/]+/", parsed.path):
                fail("comment.source_key 必须是具体 Instagram 帖子或 Reel URL")
        reason = entry.get("reason", "")
        if not isinstance(reason, str) or (state == "unavailable" and not reason.strip()):
            fail("reason 必须是文本；unavailable 必须说明原因")
        cursor = dump(entry.get("cursor", ""))
        if discovery:
            existing = db.execute("SELECT status,cursor_json,reason FROM discovery_progress WHERE kind=? AND source_key=?", (kind, key)).fetchone()
            if existing and tuple(existing) == (state, cursor, reason):
                continue
            at = now()
            db.execute("INSERT INTO discovery_progress VALUES (?,?,?,?,?,?) ON CONFLICT(kind,source_key) DO UPDATE SET status=excluded.status,cursor_json=excluded.cursor_json,reason=excluded.reason,updated_at=excluded.updated_at",
                       (kind, key, state, cursor, reason, at))
            db.execute("INSERT INTO discovery_progress_events (kind,source_key,status,cursor_json,reason,created_at) VALUES (?,?,?,?,?,?)", (kind, key, state, cursor, reason, at))
            continue
        existing = db.execute("SELECT status,cursor_json,reason FROM progress WHERE username=? AND kind=? AND source_key=?", (name, kind, key)).fetchone()
        if existing and tuple(existing) == (state, cursor, reason):
            continue
        at = now()
        db.execute("INSERT INTO progress VALUES (?,?,?,?,?,?,?) ON CONFLICT(username,kind,source_key) DO UPDATE SET status=excluded.status,cursor_json=excluded.cursor_json,reason=excluded.reason,updated_at=excluded.updated_at",
                   (name, kind, key, state, cursor, reason, at))
        db.execute("INSERT INTO progress_events (username,kind,source_key,status,cursor_json,reason,created_at) VALUES (?,?,?,?,?,?,?)", (name, kind, key, state, cursor, reason, at))
    return {"recorded": len(data["entries"])}


def discovery_views(db):
    result = []
    for row in db.execute("SELECT * FROM discovery_progress ORDER BY kind,source_key"):
        entry = dict(row)
        entry["cursor"] = json.loads(entry.pop("cursor_json"))
        result.append(entry)
    return result


def next_accounts(db, limit, include_unavailable=False):
    result = []
    for account in account_views(db):
        if account["status"] != "accepted":
            continue
        entries = {}
        for row in db.execute("SELECT * FROM progress WHERE username=? ORDER BY kind,source_key", (account["username"],)):
            entry = dict(row)
            entry["cursor"] = json.loads(entry.pop("cursor_json"))
            entries[(entry["kind"], entry["source_key"])] = entry
        for kind in ("following", "follower", "posts"):
            entries.setdefault((kind, kind), {"username": account["username"], "kind": kind, "source_key": kind, "status": "pending", "cursor": "", "reason": "尚未记录进度"})
        pending = [entry for entry in entries.values() if entry["status"] in {"pending", "in_progress"}]
        unavailable = [entry for entry in entries.values() if entry["status"] == "unavailable"]
        if pending or (include_unavailable and unavailable):
            result.append({"username": account["username"], "profile_url": account["profile_url"], "decision_origin": account["decision_origin"],
                           "independent_seed_count": account["independent_seed_count"], "entries": pending,
                           "unavailable_entries": unavailable})
        if len(result) >= limit:
            break
    states = {"pending", "in_progress", "unavailable"} if include_unavailable else {"pending", "in_progress"}
    return {"accounts": result, "discovery_entries": [entry for entry in discovery_views(db) if entry["status"] in states],
            "read_only": True, "note": "此命令不领取、不完成任务，也不重试 unavailable。posts 是帖子发现进度；每个评论帖子单独登记。"}


def csv_safe(value):
    text = "" if value is None else str(value)
    # Strip for inspection only. Keep the original text, prefixed with an apostrophe when dangerous.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def export_accounts(db, output, fmt, include_all=False):
    candidates = account_views(db)
    if not include_all:
        candidates = [item for item in candidates if item["status"] == "accepted"]
    destination = Path(output).expanduser()
    if destination.exists():
        fail("导出文件已存在；请使用新路径，以保留历史结果")
    if fmt == "json":
        text = json.dumps(candidates, ensure_ascii=False, indent=2) + "\n"
    elif fmt == "csv":
        buffer = io.StringIO(newline="")
        fields = ["username", "profile_url", "status", "decision_origin", "model_personal", "model_male", "model_resident_hk", "model_confidence", "model_reason", "reason", "independent_seed_count", "evidence", "sources"]
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        for item in candidates:
            row = {**{key: item[key] for key in fields if key in item}, **{"model_" + key: value for key, value in item["assessment"].items()}}
            row["reason"] = item["manual_reason"] if item["decision_origin"] == "human" else item["assessment"]["reason"]
            row["evidence"] = dump(item["evidence"])
            row["sources"] = dump(item["sources"])
            writer.writerow({key: csv_safe(row.get(key, "")) for key in fields})
        text = buffer.getvalue()
    else:
        def escape(value):
            return str(value).replace("&", "&amp;").replace("<", "&lt;").replace("|", "\\|").replace("\n", "<br>").replace("\r", "")
        lines = ["| 账号 | 主页 | 结果 | 判断来源 | 理由 | 独立合格种子数 | 来源路径 | 证据摘要 |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for item in candidates:
            reason = item["manual_reason"] if item["decision_origin"] == "human" else item["assessment"]["reason"]
            source_summary = "; ".join(f"{s['kind']} / source_account={s['source_account'] or '(discovery)'} / {s['source_key']} / {s['source_url']}" for s in item["sources"])
            evidence_summary = "; ".join(f"{e['criterion']} / {e['signal']}: {e['text']} ({e['url']}; observed {e['observed_at']})" for e in item["evidence"])
            lines.append("| " + " | ".join(escape(v) for v in (item["username"], item["profile_url"], item["status"], item["decision_origin"], reason, item["independent_seed_count"], source_summary, evidence_summary)) + " |")
        text = "\n".join(lines) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8-sig" if fmt == "csv" else "utf-8", newline="") as handle:
        handle.write(text)
    return {"output": str(destination.resolve()), "format": fmt, "count": len(candidates), "included_pending_and_rejected": include_all}


def stats(db):
    accounts = account_views(db)
    counts = {}
    for account in accounts:
        key = f"{account['status']}:{account['decision_origin']}"
        counts[key] = counts.get(key, 0) + 1
    comparisons = {"eligible_accepted": 0, "eligible_rejected": 0, "ineligible_accepted": 0, "ineligible_rejected": 0, "human_uncertain": 0}
    for row in db.execute("SELECT r.decision,i.snapshot_json FROM reviews r JOIN batch_items i ON i.batch_id=r.batch_id AND i.number=r.number WHERE r.id=(SELECT MAX(rr.id) FROM reviews rr WHERE rr.batch_id=r.batch_id AND rr.number=r.number)"):
        if row["decision"] == "uncertain":
            comparisons["human_uncertain"] += 1
        else:
            key = ("eligible" if json.loads(row["snapshot_json"])["model_eligible"] else "ineligible") + "_" + row["decision"]
            comparisons[key] += 1
    latest = db.execute("SELECT * FROM rules ORDER BY version DESC LIMIT 1").fetchone()
    latest_rule = None
    if latest:
        latest_rule = dict(latest)
        latest_rule["rules"] = json.loads(latest_rule.pop("rules_json"))
        latest_rule["based_on_batches"] = json.loads(latest_rule.pop("based_on_batches_json"))
    return {"mode": current_mode(db), "accounts": len(accounts), "decisions": counts,
            "sources": db.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "evidence": db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
            "completed_human_batches": completed_batches(db), "model_snapshot_vs_human": comparisons,
            "latest_rule": latest_rule,
            "discovery_progress": discovery_views(db),
            "unavailable_entries": db.execute("SELECT COUNT(*) FROM progress WHERE status='unavailable'").fetchone()[0],
            "note": "种子数只影响查看顺序，不是居港概率；自动判断不计入人工反馈。"}


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--db", required=True, help="显式指定本机 SQLite 数据库路径")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    for command in ("ingest", "review", "progress", "learn"):
        sub.add_parser(command).add_argument("json_file")
    batch = sub.add_parser("batch")
    batch.add_argument("--limit", type=int, default=20)
    batch.add_argument("--batch-id", help="只读取一个已有批次，不创建或重新编号")
    batch.add_argument("--include-reviewed", action="store_true", help="创建人工复核批次，保留以前全部反馈")
    listing = sub.add_parser("list")
    listing.add_argument("--status", choices=("accepted", "rejected", "pending"))
    exporting = sub.add_parser("export")
    exporting.add_argument("--format", choices=("csv", "json", "markdown"), required=True)
    exporting.add_argument("--output", required=True)
    exporting.add_argument("--all", action="store_true", help="包括待定和人工排除项；默认只导出入选名单")
    following = sub.add_parser("next")
    following.add_argument("--limit", type=int, default=20)
    following.add_argument("--include-unavailable", action="store_true", help="只读展示暂不可用的入口，不启动重试")
    mode = sub.add_parser("mode")
    mode.add_argument("desired", choices=MODES, nargs="?")
    mode.add_argument("--reason")
    sub.add_parser("stats")
    return root


def main(argv=None):
    arguments = parser().parse_args(argv)
    db = None
    try:
        if hasattr(arguments, "limit") and arguments.limit < 1:
            fail("--limit 必须大于零")
        db = connect(arguments.db, arguments.command == "init")
        with db:
            command = arguments.command
            if command == "init":
                result = {"database": str(Path(arguments.db).expanduser().resolve()), "mode": current_mode(db), "schema_version": 1}
            elif command in {"ingest", "review", "progress", "learn"}:
                handlers = {"ingest": ingest, "review": review, "progress": write_progress, "learn": learn}
                result = handlers[command](db, read_json(arguments.json_file))
            elif command == "batch":
                result = batch_view(db, arguments.batch_id) if arguments.batch_id else make_batch(db, arguments.limit, arguments.include_reviewed)
            elif command == "list":
                result = account_views(db)
                if arguments.status:
                    result = [item for item in result if item["status"] == arguments.status]
            elif command == "export":
                result = export_accounts(db, arguments.output, arguments.format, arguments.all)
            elif command == "next":
                result = next_accounts(db, arguments.limit, arguments.include_unavailable)
            elif command == "mode":
                result = set_mode(db, arguments.desired, arguments.reason)
            else:
                result = stats(db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    sys.exit(main())
