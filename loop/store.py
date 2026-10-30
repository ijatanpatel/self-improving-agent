"""SQLite store for issues, runs and exits (pull requests, datasets, evaluator drafts).

Issues are deduplicated by a fingerprint of the contract rule they break, not by the
agent's wording: the agent phrases a title differently every run, but the broken rule
stays the same. A re-run therefore updates the existing issue instead of adding a copy.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from loop import settings
from loop.contract import RuleResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE NOT NULL,
    rule_id TEXT NOT NULL,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open | pr_open | resolved | muted
    summary TEXT,
    root_cause TEXT,
    evidence TEXT,           -- JSON lists from here on
    code_locations TEXT,
    proposed_fix TEXT,
    evaluator_idea TEXT,
    related_rules TEXT,
    affected_requests INTEGER,
    checked_requests INTEGER,
    times_seen INTEGER NOT NULL DEFAULT 1,
    times_regressed INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    resolved_at TEXT,
    last_run_id TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    hours REAL,
    requests INTEGER,
    failing_rules TEXT,
    new_issues INTEGER DEFAULT 0,
    updated_issues INTEGER DEFAULT 0,
    resolved_issues INTEGER DEFAULT 0,
    model TEXT,
    turns INTEGER,
    tool_calls INTEGER,
    cost_usd REAL,
    transcript TEXT
);
CREATE TABLE IF NOT EXISTS exits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id),
    kind TEXT NOT NULL,      -- pr | dataset | evaluator
    target TEXT NOT NULL,    -- PR URL or branch, dataset name, evaluator file
    detail TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (issue_id, kind)  -- each exit happens at most once per issue
);
"""
JSON_COLUMNS = ("evidence", "code_locations", "proposed_fix", "evaluator_idea", "related_rules")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(app: str, rule_id: str) -> str:
    return hashlib.sha256(f"{app}|{rule_id}".encode()).hexdigest()[:12]


class IssueStore:
    def __init__(self, path: Path = settings.DB_PATH):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        for column in JSON_COLUMNS:
            if column in data and data[column] is not None:
                data[column] = json.loads(data[column])
        return data

    # ---- issues -------------------------------------------------------------------
    def upsert_finding(self, app: str, finding: dict, rule: RuleResult, run_id: str) -> str:
        """Save one finding. Returns 'new', 'updated', 'regressed' or 'muted'."""
        fp = fingerprint(app, finding["rule_id"])
        values = {
            "title": finding["title"], "severity": finding["severity"],
            "summary": finding["summary"], "root_cause": finding["root_cause"],
            "evidence": json.dumps(finding["evidence"]),
            "code_locations": json.dumps(finding["code_locations"]),
            "proposed_fix": json.dumps(finding["proposed_fix"]),
            "evaluator_idea": json.dumps(finding["evaluator_idea"]),
            "related_rules": json.dumps(finding["related_rules"]),
            "affected_requests": len(rule.violations),
            "checked_requests": rule.checked - rule.skipped,
            "last_seen": now(), "last_run_id": run_id,
        }
        with self._db() as db:
            existing = db.execute("SELECT id, status FROM issues WHERE fingerprint = ?", (fp,)).fetchone()
            if existing is None:
                db.execute(
                    f"INSERT INTO issues (fingerprint, rule_id, first_seen, {', '.join(values)}) "
                    f"VALUES (?, ?, ?, {', '.join('?' * len(values))})",
                    (fp, finding["rule_id"], now(), *values.values()),
                )
                return "new"
            outcome = {"resolved": "regressed", "muted": "muted"}.get(existing["status"], "updated")
            sets = ", ".join(f"{k} = ?" for k in values)
            db.execute(f"UPDATE issues SET {sets}, times_seen = times_seen + 1 WHERE id = ?",
                       (*values.values(), existing["id"]))
            if outcome == "regressed":  # it was fixed once and came back
                db.execute("UPDATE issues SET status = 'open', resolved_at = NULL, "
                           "times_regressed = times_regressed + 1 WHERE id = ?", (existing["id"],))
            return outcome

    def resolve_passing(self, results: list[RuleResult], min_checked: int = 5) -> list[dict]:
        """Close open issues whose rules (and related rules) all pass with enough data."""
        passing = {r.rule_id for r in results if not r.failed and r.checked - r.skipped >= min_checked}
        resolved = []
        for issue in self.list_issues():
            if issue["status"] not in ("open", "pr_open"):
                continue
            rules = [issue["rule_id"], *(issue["related_rules"] or [])]
            if all(rule in passing for rule in rules):
                with self._db() as db:
                    db.execute("UPDATE issues SET status = 'resolved', resolved_at = ? WHERE id = ?",
                               (now(), issue["id"]))
                resolved.append(issue)
        return resolved

    def list_issues(self, statuses: tuple[str, ...] | None = None) -> list[dict]:
        query, args = "SELECT * FROM issues", ()
        if statuses:
            query += f" WHERE status IN ({', '.join('?' * len(statuses))})"
            args = statuses
        with self._db() as db:
            rows = db.execute(query + " ORDER BY id", args).fetchall()
        return [self._row(r) for r in rows]

    def get_issue(self, issue_id: int) -> dict | None:
        with self._db() as db:
            return self._row(db.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone())

    def set_status(self, issue_id: int, status: str) -> None:
        with self._db() as db:
            db.execute("UPDATE issues SET status = ? WHERE id = ?", (status, issue_id))

    # ---- runs ---------------------------------------------------------------------
    def start_run(self, run_id: str, hours: float) -> None:
        with self._db() as db:
            db.execute("INSERT INTO runs (id, started_at, status, hours) VALUES (?, ?, 'running', ?)",
                       (run_id, now(), hours))

    def finish_run(self, run_id: str, **fields) -> None:
        fields = {"finished_at": now(), **fields}
        if "failing_rules" in fields:
            fields["failing_rules"] = json.dumps(fields["failing_rules"])
        with self._db() as db:
            db.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                       (*fields.values(), run_id))

    def list_runs(self) -> list[dict]:
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY started_at").fetchall()]

    # ---- exits --------------------------------------------------------------------
    def record_exit(self, issue_id: int, kind: str, target: str, detail: dict | None = None) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO exits (issue_id, kind, target, detail, created_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (issue_id, kind) DO UPDATE SET target = excluded.target, "
                "detail = excluded.detail",
                (issue_id, kind, target, json.dumps(detail or {}), now()),
            )

    def get_exit(self, issue_id: int, kind: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM exits WHERE issue_id = ? AND kind = ?",
                             (issue_id, kind)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["detail"] = json.loads(data["detail"] or "{}")
        return data

    def list_exits(self, issue_id: int) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM exits WHERE issue_id = ? ORDER BY id", (issue_id,)).fetchall()
        return [dict(r) for r in rows]
