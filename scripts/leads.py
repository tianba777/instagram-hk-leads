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

TOOL = "instagram-hk-leads"
CURRENT_SCHEMA = 3
CRITERIA = ("personal", "male", "resident_hk")
MODES = ("learning", "assisted", "automatic")
SOURCE_KINDS = {"search", "place", "following", "follower", "comment", "reply", "mention"}
RELATION_KINDS = {"following", "follower", "comment", "reply"}  # kinds whose source_account counts as a seed
PROGRESS_KINDS = {"following", "follower", "posts", "comment", "search", "place"}
PROGRESS_STATES = {"pending", "in_progress", "done", "unavailable"}
STRONG_RESIDENCE_SIGNALS = {"explicit_residence", "recent_hk_life", "self_description", "recurring_local_life"}
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
RESERVED_PATHS = {
    "p", "reel", "reels", "explore", "accounts", "stories", "tv", "direct", "tags", "locations", "about", "legal",
    "web", "challenge", "developer", "press", "api", "privacy", "terms", "emails", "session", "graphql", "ajax",
    "static", "nametag", "lite", "igtv", "guide", "guides", "ar", "oauth", "linkshim", "download", "s",
}
DECISIONS = {
    "符合": "accepted", "不符合": "rejected", "不确定": "uncertain",
    "accepted": "accepted", "rejected": "rejected", "uncertain": "uncertain",
}

# Full schema for a fresh database. Existing databases reach the same shape through MIGRATIONS.
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
    username TEXT PRIMARY KEY, profile_url TEXT NOT NULL,
    assessment_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
    assessment_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    manual_decision TEXT, manual_reason TEXT, manual_at TEXT,
    ig_user_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS accounts_ig_user_id ON accounts(ig_user_id) WHERE ig_user_id IS NOT NULL;
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
CREATE INDEX IF NOT EXISTS sources_origin ON sources(source_account);
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, mode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_items (
    batch_id TEXT NOT NULL REFERENCES batches(batch_id), number INTEGER NOT NULL,
    username TEXT NOT NULL REFERENCES accounts(username), snapshot_json TEXT NOT NULL,
    assessment_hash TEXT NOT NULL, PRIMARY KEY(batch_id, number)
);
CREATE INDEX IF NOT EXISTS batch_items_username ON batch_items(username);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, number INTEGER NOT NULL,
    username TEXT NOT NULL REFERENCES accounts(username), decision TEXT NOT NULL,
    reason TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin = 'human_feedback'),
    created_at TEXT NOT NULL,
    failed_criteria_json TEXT NOT NULL DEFAULT '[]',
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
CREATE TABLE IF NOT EXISTS seed_policy_events (
    id INTEGER PRIMARY KEY, old_allow_female INTEGER NOT NULL CHECK(old_allow_female IN (0,1)),
    new_allow_female INTEGER NOT NULL CHECK(new_allow_female IN (0,1)),
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0), created_at TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS auto_acceptances (
    username TEXT NOT NULL REFERENCES accounts(username), assessment_hash TEXT NOT NULL,
    mode TEXT NOT NULL, rule_version INTEGER NOT NULL, trigger TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(username, assessment_hash)
);
CREATE TABLE IF NOT EXISTS forgotten (
    username_hash TEXT PRIMARY KEY, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_events (
    id INTEGER PRIMARY KEY, old_username TEXT NOT NULL, new_username TEXT NOT NULL,
    reason TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

# MIGRATIONS[n] upgrades schema_version n to n+1. Each script runs with foreign keys disabled
# and inside its own transaction; `meta.schema_version` is updated by the caller.
MIGRATIONS = {
    1: """
ALTER TABLE accounts ADD COLUMN ig_user_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS accounts_ig_user_id ON accounts(ig_user_id) WHERE ig_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS sources_origin ON sources(source_account);
ALTER TABLE reviews ADD COLUMN failed_criteria_json TEXT NOT NULL DEFAULT '[]';
CREATE TABLE batch_items_v2 (
    batch_id TEXT NOT NULL REFERENCES batches(batch_id), number INTEGER NOT NULL,
    username TEXT NOT NULL REFERENCES accounts(username), snapshot_json TEXT NOT NULL,
    assessment_hash TEXT NOT NULL, PRIMARY KEY(batch_id, number)
);
INSERT INTO batch_items_v2 SELECT batch_id, number, username, snapshot_json, assessment_hash FROM batch_items;
DROP TABLE batch_items;
ALTER TABLE batch_items_v2 RENAME TO batch_items;
CREATE INDEX IF NOT EXISTS batch_items_username ON batch_items(username);
CREATE TABLE IF NOT EXISTS auto_acceptances (
    username TEXT NOT NULL REFERENCES accounts(username), assessment_hash TEXT NOT NULL,
    mode TEXT NOT NULL, rule_version INTEGER NOT NULL, trigger TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(username, assessment_hash)
);
CREATE TABLE IF NOT EXISTS forgotten (
    username_hash TEXT PRIMARY KEY, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_events (
    id INTEGER PRIMARY KEY, old_username TEXT NOT NULL, new_username TEXT NOT NULL,
    reason TEXT NOT NULL, created_at TEXT NOT NULL
);
""",
    2: """
CREATE TABLE seed_policy_events (
    id INTEGER PRIMARY KEY, old_allow_female INTEGER NOT NULL CHECK(old_allow_female IN (0,1)),
    new_allow_female INTEGER NOT NULL CHECK(new_allow_female IN (0,1)),
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0), created_at TEXT NOT NULL
);
INSERT INTO meta VALUES ('seed_allow_female', 'false');
""",
}


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


def instagram_post_path(path):
    """Return one post path for both root links and links prefixed by their owner."""
    match = re.fullmatch(r"/(?:(?P<owner>[A-Za-z0-9._]{1,30})/)?(?P<kind>p|reel|tv)/(?P<code>[A-Za-z0-9_-]+)/?", path)
    if not match:
        return None
    owner = match["owner"]
    if owner and (owner.lower() in RESERVED_PATHS or not owner.strip(".")):
        return None
    return f"/{match['kind']}/{match['code']}/"


def url(value, label="url", optional=False):
    if optional and (value is None or value == ""):
        return ""
    text = nonempty(value, label)
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        fail(f"{label} 必须是无登录凭据的 http/https URL")
    if parsed.hostname.lower() in INSTAGRAM_HOSTS:
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                 if key.lower() not in {"igsh", "igshid", "fbclid", "gclid"} and not key.lower().startswith("utm_")]
        path = instagram_post_path(parsed.path) or parsed.path.rstrip("/") + "/"
        return urlunsplit(("https", "www.instagram.com", path, urlencode(sorted(query)), ""))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def username(value):
    text = nonempty(value, "username")
    if "://" in text:
        parsed = urlsplit(text)
        if parsed.hostname not in INSTAGRAM_HOSTS:
            fail("主页 URL 必须属于 instagram.com")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1 or parts[0].lower() in RESERVED_PATHS:
            fail("需要账号主页 URL，不能使用帖子或其他页面 URL")
        text = parts[0]
    text = text.removeprefix("@").lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", text) or text in RESERVED_PATHS:
        fail("username 只能包含 1–30 个英文字母、数字、点或下划线，且不能是 Instagram 保留路径")
    return text


def ig_user_id(value):
    if value is None or value == "":
        return None
    text = str(value).strip() if isinstance(value, (str, int)) and type(value) is not bool else ""
    if not re.fullmatch(r"[0-9]{1,20}", text):
        fail("ig_user_id 必须是 Instagram 数字用户 ID")
    return text


def username_hash(name):
    return hashlib.sha256(f"{TOOL}:{name}".encode("utf-8")).hexdigest()


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


class LeadsConnection(sqlite3.Connection):
    """sqlite3.Connection that remembers which migrations ran while opening the database."""

    migrations_applied = ()


def schema_version(db):
    try:
        metadata = dict(db.execute("SELECT key,value FROM meta"))
    except sqlite3.Error:
        return None
    if metadata.get("tool") != TOOL:
        return None
    try:
        return int(metadata.get("schema_version", ""))
    except ValueError:
        return None


def migrate(db, from_version):
    applied = []
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        for version in range(from_version, CURRENT_SCHEMA):
            script = MIGRATIONS.get(version)
            if script is None:
                fail(f"缺少从版本 {version} 升级的迁移脚本")
            db.executescript("BEGIN;\n" + script + f"\nUPDATE meta SET value='{version + 1}' WHERE key='schema_version';")
            if db.execute("PRAGMA foreign_key_check").fetchall():
                db.execute("ROLLBACK")
                fail(f"从版本 {version} 升级后外键检查失败；已回退，数据库未修改")
            db.execute("COMMIT")
            applied.append(f"{version}->{version + 1}")
    finally:
        db.execute("PRAGMA foreign_keys=ON")
    return applied


def connect(path, initialize=False, allow_migration=False):
    target = Path(path).expanduser()
    if not initialize and not target.is_file():
        fail("数据库不存在；请使用显式 --db 路径运行 init")
    existing_nonempty = target.is_file() and target.stat().st_size > 0
    if initialize and not existing_nonempty:
        target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(target, factory=LeadsConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    # Verify ownership before any schema/metadata write. Never initialize over another database.
    version = None
    if existing_nonempty or not initialize:
        version = schema_version(db)
        if version is None:
            db.close()
            fail("该文件不是本工具支持的 leads 数据库；未修改文件")
        if version > CURRENT_SCHEMA:
            db.close()
            fail(f"数据库版本 {version} 高于本工具支持的 {CURRENT_SCHEMA}；请升级 skill")
    db.migrations_applied = []
    if version is not None and version < CURRENT_SCHEMA:
        if not (initialize or allow_migration):
            db.close()
            fail(f"数据库是旧版本 {version}（当前 {CURRENT_SCHEMA}）；请先运行 migrate 或 init 升级，升级前建议备份")
        db.migrations_applied = migrate(db, version)
    if initialize:
        db.executescript(SCHEMA)
        db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(CURRENT_SCHEMA),))
        db.execute("INSERT OR IGNORE INTO meta VALUES ('tool', ?)", (TOOL,))
        db.execute("INSERT OR IGNORE INTO meta VALUES ('mode', 'learning')")
        db.execute("INSERT OR IGNORE INTO meta VALUES ('seed_allow_female', 'false')")
        db.commit()
    if schema_version(db) != CURRENT_SCHEMA:
        db.close()
        fail("数据库版本检查失败")
    return db


def current_mode(db):
    return db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()[0]


def current_seed_policy(db):
    return {"allow_female": db.execute("SELECT value FROM meta WHERE key='seed_allow_female'").fetchone()[0] == "true"}


def set_seed_policy(db, allow_female=None, reason=None):
    """Change discovery eligibility only; never turn a rejected account into a client."""
    previous = current_seed_policy(db)["allow_female"]
    if allow_female is not None:
        if type(allow_female) is not bool:
            fail("seed-policy allow_female 必须是布尔值")
        reason = nonempty(reason, "seed-policy --reason")
    changed = allow_female is not None and allow_female != previous
    if changed:
        db.execute("UPDATE meta SET value=? WHERE key='seed_allow_female'", (dump(allow_female),))
        db.execute("INSERT INTO seed_policy_events (old_allow_female,new_allow_female,reason,created_at) VALUES (?,?,?,?)",
                   (int(previous), int(allow_female), reason, now()))
    events = []
    for row in db.execute("SELECT * FROM seed_policy_events ORDER BY id"):
        event = dict(row)
        for key in ("old_allow_female", "new_allow_female"):
            event[key] = bool(event[key])
        events.append(event)
    return {**current_seed_policy(db), "changed": changed, "events": events,
            "note": "女生仅可作为寻找客户的种子，仍不进入男性客户名单；只采纳最新人工反馈中仅 male 不符合的账号。"}


def latest_rule_version(db):
    return db.execute("SELECT COALESCE(MAX(version),0) FROM rules").fetchone()[0]


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
    """Return (passed, reasons, blockers). blockers are stable codes for statistics."""
    reasons, blockers = [], []
    for criterion in CRITERIA:
        if assessment[criterion] != "yes":
            reasons.append(f"{criterion} 不是 yes")
            blockers.append(f"{criterion}:{assessment[criterion]}")
    if assessment["confidence"] != "clear":
        reasons.append("confidence 不是 clear")
        blockers.append("confidence:uncertain")
    if not assessment["reason"].strip():
        reasons.append("缺少判断理由")
        blockers.append("reason:missing")
    for criterion in CRITERIA:
        matching = [e for e in evidence if e["criterion"] == criterion and e["text"].strip() and e["url"] and e["observed_at"]]
        if not matching:
            reasons.append(f"{criterion} 缺少可追溯证据")
            blockers.append(f"{criterion}:no_evidence")
        elif criterion == "resident_hk" and not any(e["signal"] in STRONG_RESIDENCE_SIGNALS for e in matching):
            reasons.append("缺少明确现居自述或近期持续香港生活证据；未知 signal 默认不能自动通过")
            blockers.append("resident_hk:weak_signal")
    return not reasons, reasons, blockers


def status(row, mode):
    if row["manual_decision"]:
        decision = row["manual_decision"]
        return ("pending" if decision == "uncertain" else decision), "human"
    passed, _, _ = model_gate(json.loads(row["assessment_json"]), json.loads(row["evidence_json"]))
    if mode != "learning" and passed:
        return "accepted", "automatic"
    return "pending", "model_pending"


def record_auto_acceptance(db, name, assessment_hash, trigger):
    """Persist the fact that an account was auto-accepted, so mode downgrades or re-assessments can be audited."""
    return db.execute(
        "INSERT OR IGNORE INTO auto_acceptances VALUES (?,?,?,?,?,?)",
        (name, assessment_hash, current_mode(db), latest_rule_version(db), trigger, now())).rowcount


def account_views(db):
    mode = current_mode(db)
    rows = db.execute("SELECT * FROM accounts").fetchall()
    states = {r["username"]: status(r, mode) for r in rows}
    # Gender-only human rejection is discovery permission, not a change to client criteria.
    latest_reviews = {r["username"]: r for r in db.execute(
        "SELECT r.* FROM reviews r WHERE r.id=(SELECT MAX(rr.id) FROM reviews rr WHERE rr.username=r.username)")}
    allow_female = current_seed_policy(db)["allow_female"]
    seed_origins = {}
    for row in rows:
        name = row["username"]
        latest = latest_reviews.get(name)
        if states[name][0] == "accepted":
            seed_origins[name] = "accepted_client"
        elif (allow_female and states[name][0] == "rejected" and latest
              and latest["decision"] == "rejected" and json.loads(latest["failed_criteria_json"]) == ["male"]):
            seed_origins[name] = "human_female_feedback"
    all_sources = {}
    for source in db.execute("SELECT * FROM sources ORDER BY first_seen, source_id"):
        all_sources.setdefault(source["username"], []).append(dict(source))
    auto_rows = {}
    for row in db.execute("SELECT username, assessment_hash, created_at FROM auto_acceptances ORDER BY created_at"):
        auto_rows.setdefault(row["username"], []).append({"assessment_hash": row["assessment_hash"], "created_at": row["created_at"]})
    result = []
    for row in rows:
        item = dict(row)
        item["assessment"] = json.loads(item.pop("assessment_json"))
        item["evidence"] = json.loads(item.pop("evidence_json"))
        item["status"], item["decision_origin"] = states[row["username"]]
        item["seed_origin"] = seed_origins.get(row["username"])
        item["seed_eligible"] = item["seed_origin"] is not None
        item["seed_only"] = item["seed_eligible"] and item["status"] != "accepted"
        item["model_eligible"], item["model_gate_reasons"], item["model_gate_blockers"] = model_gate(item["assessment"], item["evidence"])
        item["sources"] = all_sources.get(row["username"], [])
        # Count eligible, distinct relation origins; repeated paths and mentions are not extra seeds.
        seeds = sorted({s["source_account"] for s in item["sources"]
                        if s["kind"] in RELATION_KINDS
                        and s["source_account"] != row["username"]
                        and s["source_account"] in seed_origins})
        item["independent_seed_count"] = len(seeds)
        item["independent_seeds"] = seeds
        history = auto_rows.get(row["username"], [])
        item["auto_accepted_before"] = bool(history)
        item["auto_acceptance_needs_review"] = bool(history) and item["status"] != "accepted" and not item["manual_decision"]
        result.append(item)
    return sorted(result, key=lambda item: (-item["independent_seed_count"], item["created_at"], item["username"]))


def ingest(db, data):
    inserted = updated = source_count = evidence_count = auto_count = 0
    mode = current_mode(db)
    for incoming in records(data, "accounts"):
        name = username(incoming.get("username") or incoming.get("profile_url"))
        if incoming.get("profile_url") and username(incoming["profile_url"]) != name:
            fail("username 与 profile_url 不一致")
        if db.execute("SELECT 1 FROM forgotten WHERE username_hash=?", (username_hash(name),)).fetchone():
            fail(f"账号 {name} 已被 forget 删除并列入拒绝重新导入名单；如确需恢复，先运行 forget --unblock")
        existing = db.execute("SELECT * FROM accounts WHERE username=?", (name,)).fetchone()
        user_id = ig_user_id(incoming.get("ig_user_id"))
        if user_id:
            owner = db.execute("SELECT username FROM accounts WHERE ig_user_id=? AND username!=?", (user_id, name)).fetchone()
            if owner:
                fail(f"ig_user_id {user_id} 已属于账号 {owner[0]}；若为同一人改名，请先运行 merge {owner[0]} {name}")
            if existing and existing["ig_user_id"] and existing["ig_user_id"] != user_id:
                fail(f"账号 {name} 已记录不同的 ig_user_id {existing['ig_user_id']}；不能静默覆盖")
        has_assessment = "assessment" in incoming
        assessment = normalize_assessment(incoming["assessment"]) if has_assessment else (
            json.loads(existing["assessment_json"]) if existing else normalize_assessment({}))
        incoming_evidence = normalize_evidence(incoming.get("evidence", []))
        # New assessments must carry their own evidence; old observations cannot silently prove a new judgment.
        evidence = incoming_evidence if has_assessment or not existing else json.loads(existing["evidence_json"])
        assessment_hash = digest([assessment, evidence])
        changed = not existing or existing["assessment_hash"] != assessment_hash
        if existing and changed:
            previous_hash = digest([json.loads(existing["assessment_json"]),
                                    normalize_evidence(json.loads(existing["evidence_json"]))])
            if assessment_hash == previous_hash:
                # URL spelling alone must not invalidate a previous judgment or its batch review.
                assessment_hash = existing["assessment_hash"]
                changed = False
        observed = now()
        if not existing:
            db.execute("INSERT INTO accounts (username,profile_url,assessment_json,evidence_json,assessment_hash,created_at,updated_at,ig_user_id) VALUES (?,?,?,?,?,?,?,?)",
                       (name, f"https://www.instagram.com/{name}/", dump(assessment), dump(evidence), assessment_hash, observed, observed, user_id))
            inserted += 1
        else:
            if changed:
                db.execute("UPDATE accounts SET assessment_json=?,evidence_json=?,assessment_hash=?,updated_at=? WHERE username=?",
                           (dump(assessment), dump(evidence), assessment_hash, observed, name))
                updated += 1
            if user_id and not existing["ig_user_id"]:
                db.execute("UPDATE accounts SET ig_user_id=? WHERE username=?", (user_id, name))
        if changed:
            db.execute("INSERT INTO assessments (username,assessment_json,evidence_json,assessment_hash,created_at) VALUES (?,?,?,?,?)",
                       (name, dump(assessment), dump(evidence), assessment_hash, observed))
        manual = existing["manual_decision"] if existing else None
        if mode != "learning" and not manual and model_gate(assessment, evidence)[0]:
            auto_count += record_auto_acceptance(db, name, assessment_hash, "ingest")
        known_evidence = set()
        if incoming_evidence:
            previous_evidence = [dict(row) for row in db.execute(
                "SELECT criterion,signal,text,url,observed_at FROM evidence WHERE username=?", (name,))]
            known_evidence = {digest([name, entry]) for entry in normalize_evidence(previous_evidence)}
        for entry in incoming_evidence:
            evidence_id = digest([name, entry])
            if evidence_id in known_evidence:
                continue
            evidence_count += db.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?,?)",
                                         (evidence_id, name, entry["criterion"], entry["signal"], entry["text"], entry["url"], entry["observed_at"])).rowcount
            known_evidence.add(evidence_id)
        values = incoming.get("sources", [])
        if not isinstance(values, list):
            fail("sources 必须是列表")
        for source in values:
            if not isinstance(source, dict) or source.get("kind") not in SOURCE_KINDS:
                fail("sources.kind 不受支持")
            kind = source["kind"]
            origin = username(source["source_account"]) if source.get("source_account") else ""
            if kind in RELATION_KINDS | {"mention"} and not origin:
                fail(f"{kind} 来源必须提供 source_account")
            source_url = url(source.get("source_url"), "sources.source_url")
            key = kind if kind in {"following", "follower"} else nonempty(source.get("source_key") or source_url, "sources.source_key")
            if "://" in key:
                key = url(key, "sources.source_key")
            seen = timestamp(source.get("observed_at"), "sources.observed_at")
            source_id = digest([name, kind, origin, key])
            previous = db.execute("SELECT source_id,first_seen,last_seen FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if previous is None and "://" in key:
                # Keep legacy source rows and their history; only compare equivalent URLs in memory.
                previous = next((row for row in db.execute(
                    "SELECT source_id,source_key,first_seen,last_seen FROM sources WHERE username=? AND kind=? AND source_account=?",
                    (name, kind, origin)) if "://" in row["source_key"] and url(row["source_key"]) == key), None)
            if previous:
                db.execute("UPDATE sources SET first_seen=?,last_seen=? WHERE source_id=?",
                           (min(previous["first_seen"], seen), max(previous["last_seen"], seen), previous["source_id"]))
            else:
                db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)", (source_id, name, kind, origin, source_url, key, seen, seen))
                source_count += 1
    return {"inserted_accounts": inserted, "updated_assessments": updated, "new_sources": source_count,
            "new_evidence": evidence_count, "new_auto_acceptances": auto_count, "mode": mode}


def batch_view(db, batch_id):
    batch = db.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if not batch:
        fail("batch_id 不存在")
    items = []
    for row in db.execute("SELECT * FROM batch_items WHERE batch_id=? ORDER BY number", (batch_id,)):
        entry = {"number": row["number"], "username": row["username"], "snapshot": json.loads(row["snapshot_json"])}
        entry["reviews"] = []
        for r in db.execute("SELECT id,decision,reason,failed_criteria_json,origin,created_at FROM reviews WHERE batch_id=? AND number=? ORDER BY id", (batch_id, row["number"])):
            review_row = dict(r)
            review_row["failed_criteria"] = json.loads(review_row.pop("failed_criteria_json"))
            entry["reviews"].append(review_row)
        items.append(entry)
    return {**dict(batch), "items": items}


def list_batches(db):
    rows = db.execute("""
        SELECT b.batch_id, b.created_at, b.mode,
               (SELECT COUNT(*) FROM batch_items i WHERE i.batch_id=b.batch_id) AS item_count,
               (SELECT COUNT(*) FROM batch_items i WHERE i.batch_id=b.batch_id
                  AND EXISTS (SELECT 1 FROM reviews r WHERE r.batch_id=i.batch_id AND r.number=i.number)) AS reviewed_count
        FROM batches b ORDER BY b.created_at""").fetchall()
    result = []
    for row in rows:
        entry = dict(row)
        entry["open"] = entry["reviewed_count"] < entry["item_count"]
        entry["pending_numbers"] = [r[0] for r in db.execute(
            "SELECT number FROM batch_items i WHERE batch_id=? AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.batch_id=i.batch_id AND r.number=i.number) ORDER BY number",
            (row["batch_id"],))]
        result.append(entry)
    return {"batches": result, "open_batches": [b["batch_id"] for b in result if b["open"]]}


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


def normalize_failed_criteria(value, decision):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        fail("review.failed_criteria 必须是文本列表")
    invalid = sorted(set(value) - set(CRITERIA))
    if invalid:
        fail(f"review.failed_criteria 只能包含 {', '.join(CRITERIA)}；无效项：{', '.join(invalid)}")
    if value and decision == "accepted":
        fail("符合的账号不能同时标记 failed_criteria")
    return sorted(set(value))


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
        failed = normalize_failed_criteria(entry.get("failed_criteria"), decision)
        previous = db.execute("SELECT decision,reason,failed_criteria_json FROM reviews WHERE batch_id=? AND number=? ORDER BY id DESC LIMIT 1", (batch_id, entry["number"])).fetchone()
        if previous and tuple(previous) == (decision, reason, dump(failed)):
            continue
        at = now()
        db.execute("INSERT INTO reviews (batch_id,number,username,decision,reason,origin,created_at,failed_criteria_json) VALUES (?,?,?,?,?,'human_feedback',?,?)",
                   (batch_id, entry["number"], item["username"], decision, reason, at, dump(failed)))
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
    increasing = MODES.index(desired) > MODES.index(previous)
    if increasing and len(complete) < 2:
        fail("提高自动程度需要至少 2 个完整的人工审核批次；模型判断或自动入选不计入")
    db.execute("UPDATE meta SET value=? WHERE key='mode'", (desired,))
    db.execute("INSERT INTO mode_events (old_mode,new_mode,reason,completed_batches_json,created_at) VALUES (?,?,?,?,?)",
               (previous, desired, reason, dump(complete), now()))
    result = {"previous_mode": previous, "mode": desired, "reason": reason, "completed_human_batches": complete,
              "note": "仅更改审核方式和记录依据；没有训练模型。未知或证据不足的账号仍待定。"}
    if previous == "learning" and desired != "learning":
        newly = 0
        for row in db.execute("SELECT username, assessment_json, evidence_json, assessment_hash FROM accounts WHERE manual_decision IS NULL"):
            if model_gate(json.loads(row["assessment_json"]), json.loads(row["evidence_json"]))[0]:
                newly += record_auto_acceptance(db, row["username"], row["assessment_hash"], "mode_change")
        result["new_auto_acceptances"] = newly
    if desired == "learning":
        affected = [r[0] for r in db.execute(
            "SELECT DISTINCT a.username FROM auto_acceptances a JOIN accounts acc ON acc.username=a.username "
            "WHERE acc.manual_decision IS NULL ORDER BY a.username")]
        result["auto_accepted_now_pending"] = affected
        result["note"] += " 降级后此前自动入选的账号回到待审；它们作为种子扩展出的账号也会失去对应加权，可用 batch --include-reviewed 复核。"
    return result


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
    version = latest_rule_version(db) + 1
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
            # Validate the observed path before generic URL cleanup can hide malformed slashes.
            parsed = urlsplit(entry["source_key"].strip())
            if parsed.hostname.lower() not in INSTAGRAM_HOSTS or instagram_post_path(parsed.path) is None:
                fail("comment.source_key 必须是具体 Instagram 帖子或 Reel URL")
        elif discovery and "://" in key:
            key = url(key, "discovery.source_key")
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
        if not account["seed_eligible"]:
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
                           "status": account["status"], "client_status": account["status"],
                           "seed_eligible": account["seed_eligible"], "seed_only": account["seed_only"], "seed_origin": account["seed_origin"],
                           "independent_seed_count": account["independent_seed_count"], "entries": pending,
                           "unavailable_entries": unavailable})
            if len(result) >= limit:
                break
    states = {"pending", "in_progress", "unavailable"} if include_unavailable else {"pending", "in_progress"}
    return {"accounts": result, "discovery_entries": [entry for entry in discovery_views(db) if entry["status"] in states],
            "read_only": True, "note": "此命令不领取、不完成任务，也不重试 unavailable。posts 是帖子发现进度；每个评论帖子单独登记。"}


def rewrite_snapshots(db, old_name, new_name=None):
    """Batch snapshots are frozen copies, but they must not keep a forgotten or renamed username.

    With new_name=None every reference to old_name is removed; otherwise references are renamed.
    """
    touched = 0
    for row in db.execute("SELECT batch_id, number, snapshot_json FROM batch_items WHERE instr(snapshot_json, ?) > 0", (old_name,)).fetchall():
        snapshot = json.loads(row["snapshot_json"])
        if snapshot.get("username") == old_name and new_name:
            snapshot["username"] = new_name
            snapshot["profile_url"] = f"https://www.instagram.com/{new_name}/"
        sources = []
        for source in snapshot.get("sources", []):
            if source.get("source_account") == old_name or source.get("username") == old_name:
                if new_name is None:
                    continue
                source = {**source, "source_account": new_name if source.get("source_account") == old_name else source.get("source_account"),
                          "username": new_name if source.get("username") == old_name else source.get("username")}
            sources.append(source)
        snapshot["sources"] = sources
        seeds = [new_name if seed == old_name and new_name else seed for seed in snapshot.get("independent_seeds", []) if seed != old_name or new_name]
        snapshot["independent_seeds"] = sorted(set(seeds))
        snapshot["independent_seed_count"] = len(snapshot["independent_seeds"])
        db.execute("UPDATE batch_items SET snapshot_json=? WHERE batch_id=? AND number=?", (dump(snapshot), row["batch_id"], row["number"]))
        touched += 1
    return touched


def forget(db, name, reason, unblock=False):
    """Erase every record about one account. Only a salted hash remains, to refuse silent re-import."""
    name = username(name)
    key = username_hash(name)
    if unblock:
        removed = db.execute("DELETE FROM forgotten WHERE username_hash=?", (key,)).rowcount
        return {"username": name, "unblocked": bool(removed)}
    reason = nonempty(reason, "forget --reason")
    exists = db.execute("SELECT 1 FROM accounts WHERE username=?", (name,)).fetchone()
    counts = {}
    counts["reviews"] = db.execute("DELETE FROM reviews WHERE username=?", (name,)).rowcount
    counts["batch_items"] = db.execute("DELETE FROM batch_items WHERE username=?", (name,)).rowcount
    counts["auto_acceptances"] = db.execute("DELETE FROM auto_acceptances WHERE username=?", (name,)).rowcount
    counts["progress"] = db.execute("DELETE FROM progress WHERE username=?", (name,)).rowcount
    counts["progress_events"] = db.execute("DELETE FROM progress_events WHERE username=?", (name,)).rowcount
    counts["evidence"] = db.execute("DELETE FROM evidence WHERE username=?", (name,)).rowcount
    counts["assessments"] = db.execute("DELETE FROM assessments WHERE username=?", (name,)).rowcount
    # Relations in both directions mention this account; both are personal data about it.
    counts["sources_about"] = db.execute("DELETE FROM sources WHERE username=?", (name,)).rowcount
    counts["sources_as_seed"] = db.execute("DELETE FROM sources WHERE source_account=?", (name,)).rowcount
    counts["merge_events"] = db.execute("DELETE FROM merge_events WHERE old_username=? OR new_username=?", (name, name)).rowcount
    counts["accounts"] = db.execute("DELETE FROM accounts WHERE username=?", (name,)).rowcount
    counts["snapshots_scrubbed"] = rewrite_snapshots(db, name)
    db.execute("INSERT OR REPLACE INTO forgotten VALUES (?,?,?)", (key, reason, now()))
    return {"username": name, "existed": bool(exists), "deleted": counts,
            "note": "已删除该账号的全部记录，仅保留用户名哈希以拒绝再次导入；批次中的编号随之消失，其他账号快照里指向它的来源也已移除。"}


def merge(db, old_name, new_name, reason):
    """Move every record from old_name to new_name (Instagram rename). Works whether or not new_name already exists."""
    old_name, new_name = username(old_name), username(new_name)
    reason = nonempty(reason, "merge --reason")
    if old_name == new_name:
        fail("merge 的两个用户名相同")
    old = db.execute("SELECT * FROM accounts WHERE username=?", (old_name,)).fetchone()
    if not old:
        fail(f"账号不存在：{old_name}")
    if db.execute("SELECT 1 FROM forgotten WHERE username_hash=?", (username_hash(new_name),)).fetchone():
        fail(f"目标账号 {new_name} 已被 forget 删除并列入拒绝重新导入名单")
    new = db.execute("SELECT * FROM accounts WHERE username=?", (new_name,)).fetchone()
    at = now()
    # Release the unique ig_user_id before it is re-attached to new_name.
    db.execute("UPDATE accounts SET ig_user_id=NULL WHERE username=?", (old_name,))
    if new:
        if old["ig_user_id"] and new["ig_user_id"] and old["ig_user_id"] != new["ig_user_id"]:
            fail("两个账号记录了不同的 ig_user_id，不是同一个人，拒绝合并")
        if old["manual_decision"] and new["manual_decision"] and old["manual_decision"] != new["manual_decision"]:
            fail("两个账号的人工结论不同；请先用 review 统一结论，再合并")
        # Keep the most recent assessment and matching latest human conclusion/reason.
        keep_old_assessment = old["updated_at"] > new["updated_at"]
        assessment_json = old["assessment_json"] if keep_old_assessment else new["assessment_json"]
        evidence_json = old["evidence_json"] if keep_old_assessment else new["evidence_json"]
        manual = (old["manual_decision"], old["manual_reason"], old["manual_at"]) if old["manual_decision"] and (
            not new["manual_decision"] or (old["manual_at"] or "") > (new["manual_at"] or "")) \
            else (new["manual_decision"], new["manual_reason"], new["manual_at"])
        db.execute("UPDATE accounts SET assessment_json=?,evidence_json=?,assessment_hash=?,created_at=?,updated_at=?,"
                   "manual_decision=?,manual_reason=?,manual_at=?,ig_user_id=COALESCE(ig_user_id,?) WHERE username=?",
                   (assessment_json, evidence_json, digest([json.loads(assessment_json), json.loads(evidence_json)]),
                    min(old["created_at"], new["created_at"]), max(old["updated_at"], new["updated_at"]),
                    *manual, old["ig_user_id"], new_name))
    else:
        db.execute("INSERT INTO accounts (username,profile_url,assessment_json,evidence_json,assessment_hash,created_at,updated_at,manual_decision,manual_reason,manual_at,ig_user_id) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (new_name, f"https://www.instagram.com/{new_name}/", old["assessment_json"], old["evidence_json"], old["assessment_hash"],
                    old["created_at"], old["updated_at"], old["manual_decision"], old["manual_reason"], old["manual_at"], old["ig_user_id"]))
    # Simple ownership moves.
    db.execute("UPDATE assessments SET username=? WHERE username=?", (new_name, old_name))
    db.execute("UPDATE batch_items SET username=? WHERE username=?", (new_name, old_name))
    db.execute("UPDATE reviews SET username=? WHERE username=?", (new_name, old_name))
    db.execute("UPDATE progress_events SET username=? WHERE username=?", (new_name, old_name))
    db.execute("UPDATE merge_events SET new_username=? WHERE new_username=?", (new_name, old_name))
    # Content-addressed rows must be re-keyed and de-duplicated.
    for row in db.execute("SELECT * FROM evidence WHERE username=?", (old_name,)).fetchall():
        entry = {k: row[k] for k in ("criterion", "signal", "text", "url", "observed_at")}
        db.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?,?)",
                   (digest([new_name, entry]), new_name, entry["criterion"], entry["signal"], entry["text"], entry["url"], entry["observed_at"]))
    db.execute("DELETE FROM evidence WHERE username=?", (old_name,))
    for row in db.execute("SELECT * FROM sources WHERE username=? OR source_account=?", (old_name, old_name)).fetchall():
        target = new_name if row["username"] == old_name else row["username"]
        origin = new_name if row["source_account"] == old_name else row["source_account"]
        if target == origin:
            db.execute("DELETE FROM sources WHERE source_id=?", (row["source_id"],))
            continue
        source_id = digest([target, row["kind"], origin, row["source_key"]])
        previous = db.execute("SELECT first_seen,last_seen FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if previous:
            db.execute("UPDATE sources SET first_seen=?,last_seen=? WHERE source_id=?",
                       (min(previous[0], row["first_seen"]), max(previous[1], row["last_seen"]), source_id))
            db.execute("DELETE FROM sources WHERE source_id=?", (row["source_id"],))
        else:
            db.execute("UPDATE sources SET source_id=?,username=?,source_account=? WHERE source_id=?", (source_id, target, origin, row["source_id"]))
    for row in db.execute("SELECT * FROM progress WHERE username=?", (old_name,)).fetchall():
        existing = db.execute("SELECT updated_at FROM progress WHERE username=? AND kind=? AND source_key=?", (new_name, row["kind"], row["source_key"])).fetchone()
        if existing and existing[0] >= row["updated_at"]:
            continue
        db.execute("INSERT INTO progress VALUES (?,?,?,?,?,?,?) ON CONFLICT(username,kind,source_key) DO UPDATE SET status=excluded.status,cursor_json=excluded.cursor_json,reason=excluded.reason,updated_at=excluded.updated_at",
                   (new_name, row["kind"], row["source_key"], row["status"], row["cursor_json"], row["reason"], row["updated_at"]))
    db.execute("DELETE FROM progress WHERE username=?", (old_name,))
    db.execute("UPDATE OR IGNORE auto_acceptances SET username=? WHERE username=?", (new_name, old_name))
    db.execute("DELETE FROM auto_acceptances WHERE username=?", (old_name,))
    db.execute("DELETE FROM accounts WHERE username=?", (old_name,))
    rewrite_snapshots(db, old_name, new_name)
    db.execute("INSERT INTO merge_events (old_username,new_username,reason,created_at) VALUES (?,?,?,?)", (old_name, new_name, reason, at))
    view = next(item for item in account_views(db) if item["username"] == new_name)
    return {"old_username": old_name, "new_username": new_name, "merged_into_existing": bool(new), "reason": reason,
            "status": view["status"], "decision_origin": view["decision_origin"], "sources": len(view["sources"]),
            "note": "历史批次中的编号保持不变，只是指向新用户名；人工结论与证据均已合并保留。"}


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
        fields = ["username", "profile_url", "ig_user_id", "status", "decision_origin", "model_personal", "model_male", "model_resident_hk", "model_confidence", "model_reason", "reason", "independent_seed_count", "evidence", "sources"]
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
    gate_blockers = {}
    for account in accounts:
        key = f"{account['status']}:{account['decision_origin']}"
        counts[key] = counts.get(key, 0) + 1
        if account["status"] == "pending" and not account["manual_decision"]:
            for blocker in account["model_gate_blockers"]:
                gate_blockers[blocker] = gate_blockers.get(blocker, 0) + 1
    comparisons = {"eligible_accepted": 0, "eligible_rejected": 0, "ineligible_accepted": 0, "ineligible_rejected": 0, "human_uncertain": 0}
    failed_criteria = {criterion: 0 for criterion in CRITERIA}
    rejected_without_criteria = 0
    for row in db.execute("SELECT r.decision,r.failed_criteria_json,i.snapshot_json FROM reviews r JOIN batch_items i ON i.batch_id=r.batch_id AND i.number=r.number WHERE r.id=(SELECT MAX(rr.id) FROM reviews rr WHERE rr.batch_id=r.batch_id AND rr.number=r.number)"):
        if row["decision"] == "uncertain":
            comparisons["human_uncertain"] += 1
        else:
            key = ("eligible" if json.loads(row["snapshot_json"])["model_eligible"] else "ineligible") + "_" + row["decision"]
            comparisons[key] += 1
        if row["decision"] == "rejected":
            failed = json.loads(row["failed_criteria_json"])
            for criterion in failed:
                failed_criteria[criterion] += 1
            if not failed:
                rejected_without_criteria += 1
    latest = db.execute("SELECT * FROM rules ORDER BY version DESC LIMIT 1").fetchone()
    latest_rule = None
    if latest:
        latest_rule = dict(latest)
        latest_rule["rules"] = json.loads(latest_rule.pop("rules_json"))
        latest_rule["based_on_batches"] = json.loads(latest_rule.pop("based_on_batches_json"))
    batches = list_batches(db)
    return {"mode": current_mode(db), "schema_version": CURRENT_SCHEMA, "accounts": len(accounts), "decisions": counts,
            "seed_policy": current_seed_policy(db),
            "seed_eligible_count": sum(a["seed_eligible"] for a in accounts),
            "seed_only_accounts": sorted(a["username"] for a in accounts if a["seed_only"]),
            "sources": db.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "evidence": db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
            "open_batches": batches["open_batches"],
            "completed_human_batches": completed_batches(db), "model_snapshot_vs_human": comparisons,
            "human_rejections_by_criterion": {**failed_criteria, "unspecified": rejected_without_criteria},
            "pending_gate_blockers": dict(sorted(gate_blockers.items())),
            "auto_acceptances": db.execute("SELECT COUNT(*) FROM auto_acceptances").fetchone()[0],
            "auto_acceptances_needing_review": [a["username"] for a in accounts if a["auto_acceptance_needs_review"]],
            "forgotten": db.execute("SELECT COUNT(*) FROM forgotten").fetchone()[0],
            "latest_rule": latest_rule,
            "discovery_progress": discovery_views(db),
            "unavailable_entries": db.execute("SELECT COUNT(*) FROM progress WHERE status='unavailable'").fetchone()[0],
            "note": "种子数只影响查看顺序，不是居港概率；自动判断不计入人工反馈。pending_gate_blockers 统计待审账号被固定检查挡住的原因，用于判断自动模式是否可行。"}


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--db", required=True, help="显式指定本机 SQLite 数据库路径")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="创建数据库；对旧版本数据库执行升级")
    sub.add_parser("migrate", help="仅升级旧版本数据库结构，不做其他改动")
    for command in ("ingest", "review", "progress", "learn"):
        sub.add_parser(command).add_argument("json_file")
    batch = sub.add_parser("batch")
    batch.add_argument("--limit", type=int, default=20)
    batch.add_argument("--batch-id", help="只读取一个已有批次，不创建或重新编号")
    batch.add_argument("--list", action="store_true", help="列出全部批次及未反馈编号，不创建批次")
    batch.add_argument("--include-reviewed", action="store_true", help="创建人工复核批次，保留以前全部反馈")
    listing = sub.add_parser("list")
    listing.add_argument("--status", choices=("accepted", "rejected", "pending"))
    listing.add_argument("--seeds", action="store_true", help="仅展示可用于寻找客户的种子；女生种子仍标记为 rejected 客户")
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
    seed_policy = sub.add_parser("seed-policy", help="读取或修改种子策略，不更改客户标准和审核方式")
    policy_choice = seed_policy.add_mutually_exclusive_group()
    policy_choice.add_argument("--allow-female", dest="allow_female", action="store_const", const=True, default=None)
    policy_choice.add_argument("--disallow-female", dest="allow_female", action="store_const", const=False)
    seed_policy.add_argument("--reason")
    forgetting = sub.add_parser("forget", help="删除一个账号的全部记录（个人资料删除请求）")
    forgetting.add_argument("username")
    forgetting.add_argument("--reason")
    forgetting.add_argument("--unblock", action="store_true", help="仅解除该账号的拒绝重新导入标记")
    merging = sub.add_parser("merge", help="Instagram 改名：把旧用户名的全部记录并入新用户名")
    merging.add_argument("old_username")
    merging.add_argument("new_username")
    merging.add_argument("--reason")
    sub.add_parser("stats")
    return root


def main(argv=None):
    # Output is UTF-8 JSON regardless of the console code page (Windows defaults to cp1252/gbk).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8")
    arguments = parser().parse_args(argv)
    db = None
    try:
        if hasattr(arguments, "limit") and arguments.limit < 1:
            fail("--limit 必须大于零")
        command = arguments.command
        db = connect(arguments.db, initialize=command == "init", allow_migration=command == "migrate")
        with db:
            if command == "init":
                result = {"database": str(Path(arguments.db).expanduser().resolve()), "mode": current_mode(db),
                          "schema_version": CURRENT_SCHEMA, "migrations_applied": db.migrations_applied}
            elif command == "migrate":
                result = {"database": str(Path(arguments.db).expanduser().resolve()), "schema_version": CURRENT_SCHEMA,
                          "migrations_applied": db.migrations_applied}
            elif command in {"ingest", "review", "progress", "learn"}:
                handlers = {"ingest": ingest, "review": review, "progress": write_progress, "learn": learn}
                result = handlers[command](db, read_json(arguments.json_file))
            elif command == "batch":
                if arguments.list:
                    result = list_batches(db)
                elif arguments.batch_id:
                    result = batch_view(db, arguments.batch_id)
                else:
                    result = make_batch(db, arguments.limit, arguments.include_reviewed)
            elif command == "list":
                result = account_views(db)
                if arguments.status:
                    result = [item for item in result if item["status"] == arguments.status]
                if arguments.seeds:
                    result = [item for item in result if item["seed_eligible"]]
            elif command == "export":
                result = export_accounts(db, arguments.output, arguments.format, arguments.all)
            elif command == "next":
                result = next_accounts(db, arguments.limit, arguments.include_unavailable)
            elif command == "mode":
                result = set_mode(db, arguments.desired, arguments.reason)
            elif command == "seed-policy":
                result = set_seed_policy(db, arguments.allow_female, arguments.reason)
            elif command == "forget":
                result = forget(db, arguments.username, arguments.reason, arguments.unblock)
            elif command == "merge":
                result = merge(db, arguments.old_username, arguments.new_username, arguments.reason)
            else:
                result = stats(db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    sys.exit(main())
