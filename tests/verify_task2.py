"""Task 2 verification script — B7 (hot-take fork) + B9 (dedupe) + A2 + C10 + C11.

Run with: uv run python tests/verify_task2.py
"""
from __future__ import annotations
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone


def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


# ---------------------------------------------------------------------------
# B7 — hot-take fork logic
# ---------------------------------------------------------------------------
section("B7: _pick_persona hot-take fork")

from xhs_op.generate.translator import _pick_persona
from xhs_op.db import Idea

now = datetime.now(timezone.utc)

def make_idea(engagement_score: float, posted_offset: timedelta) -> Idea:
    """Build a transient (not DB-persisted) Idea for testing."""
    return Idea(
        source="x",
        source_url="https://twitter.com/test/status/1",
        source_id="test_b7",
        raw_title="test",
        raw_body="test body",
        raw_lang="en",
        engagement_score=engagement_score,
        fetched_at=now,
        category="ai",
        target_account="stock",
        extra={"posted_at": (now - posted_offset).isoformat()},
        processed=False,
    )

# Case 1: age=30min, score=80 → expect stock_hottake
idea1 = make_idea(80.0, timedelta(minutes=30))
result1 = _pick_persona(idea1, threshold=50.0)
status1 = "PASS" if result1 == "stock_hottake" else "FAIL"
print(f"Case 1 (age=30m, score=80, thresh=50): _pick_persona → '{result1}' [{status1}]")

# Case 2: age=30min, score=30 → expect stock_digest
idea2 = make_idea(30.0, timedelta(minutes=30))
result2 = _pick_persona(idea2, threshold=50.0)
status2 = "PASS" if result2 == "stock_digest" else "FAIL"
print(f"Case 2 (age=30m, score=30, thresh=50): _pick_persona → '{result2}' [{status2}]")

# Case 3: age=3h, score=80 → expect stock_digest (too old)
idea3 = make_idea(80.0, timedelta(hours=3))
result3 = _pick_persona(idea3, threshold=50.0)
status3 = "PASS" if result3 == "stock_digest" else "FAIL"
print(f"Case 3 (age=3h,  score=80, thresh=50): _pick_persona → '{result3}' [{status3}]")

b7_pass = (result1 == "stock_hottake" and result2 == "stock_digest" and result3 == "stock_digest")
print(f"\nB7 overall: {'PASS' if b7_pass else 'FAIL'}")


# ---------------------------------------------------------------------------
# B9 — _insert_ideas dedupe
# ---------------------------------------------------------------------------
section("B9: _insert_ideas dedupe")

from xhs_op.db import init_db, get_session
from xhs_op.sources.x_scraper import _insert_ideas
from sqlmodel import select

init_db()

# We need to simulate a "tweet-like" object for _insert_ideas.
class FakeTweet:
    def __init__(self, tweet_id: str, text: str = "test tweet"):
        self.id = tweet_id
        self.full_text = text
        self.text = text
        self.favorite_count = 10
        self.retweet_count = 2
        self.reply_count = 1
        self.created_at_datetime = datetime.now(timezone.utc)
        self.created_at = self.created_at_datetime.isoformat()
        self.lang = "en"
        self.hashtags = []
        self.urls = []
        self.user = None

DEDUPE_ID = "test_tweet_001"

# Clear any leftover row from a previous run
with get_session() as session:
    existing = session.exec(
        select(Idea).where(Idea.source == "x", Idea.source_id == DEDUPE_ID)
    ).all()
    for row in existing:
        session.delete(row)

candidates = [(FakeTweet(DEDUPE_ID), None, "ai")]

# First insert
count1 = _insert_ideas(
    candidates, since=None, keywords={}, target_account="stock", cap=None
)
print(f"First _insert_ideas call returned: {count1}  (expected 1)")

# Second insert — same tweet_id, should be deduped
count2 = _insert_ideas(
    candidates, since=None, keywords={}, target_account="stock", cap=None
)
print(f"Second _insert_ideas call returned: {count2} (expected 0)")

# Confirm DB has exactly 1 row
with get_session() as session:
    rows = session.exec(
        select(Idea).where(Idea.source == "x", Idea.source_id == DEDUPE_ID)
    ).all()
row_count = len(rows)
print(f"DB row count for source_id='{DEDUPE_ID}': {row_count}  (expected 1)")

b9_pass = (count1 == 1 and count2 == 0 and row_count == 1)
print(f"\nB9 overall: {'PASS' if b9_pass else 'FAIL'}")


# ---------------------------------------------------------------------------
# A2 — --dry-run
# ---------------------------------------------------------------------------
section("A2: --dry-run")

import subprocess
result_a2 = subprocess.run(
    [sys.executable, "-m", "xhs_op.sources.x_scraper", "--dry-run"],
    capture_output=True, text=True
)
print(f"Exit code: {result_a2.returncode}  (expected 0)")
print(f"Stdout: {result_a2.stdout.strip()}")
print(f"Stderr: {result_a2.stderr.strip()}")
a2_pass = result_a2.returncode == 0
print(f"\nA2 overall: {'PASS' if a2_pass else 'FAIL'}")


# ---------------------------------------------------------------------------
# C10 — --help for x_scraper (confirm flags)
# ---------------------------------------------------------------------------
section("C10: x_scraper --help flags")

result_c10 = subprocess.run(
    [sys.executable, "-m", "xhs_op.sources.x_scraper", "--help"],
    capture_output=True, text=True
)
help_text = result_c10.stdout + result_c10.stderr
c10_once = "--once" in help_text
c10_limit = "--limit" in help_text
c10_dry = "--dry-run" in help_text
print(f"Exit code: {result_c10.returncode}  (expected 0)")
print(f"--once present: {c10_once}")
print(f"--limit present: {c10_limit}")
print(f"--dry-run present: {c10_dry}")
c10_pass = result_c10.returncode == 0 and c10_once and c10_limit and c10_dry
print(f"\nC10 overall: {'PASS' if c10_pass else 'FAIL'}")


# ---------------------------------------------------------------------------
# C11 — --help for translator (confirm --idea-id)
# ---------------------------------------------------------------------------
section("C11: translator --help flags")

result_c11 = subprocess.run(
    [sys.executable, "-m", "xhs_op.generate.translator", "--help"],
    capture_output=True, text=True
)
help_text_c11 = result_c11.stdout + result_c11.stderr
c11_idea_id = "--idea-id" in help_text_c11
print(f"Exit code: {result_c11.returncode}  (expected 0)")
print(f"--idea-id present: {c11_idea_id}")
print(f"Help text:\n{help_text_c11.strip()}")
c11_pass = result_c11.returncode == 0 and c11_idea_id
print(f"\nC11 overall: {'PASS' if c11_pass else 'FAIL'}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
section("SUMMARY")

results = {
    "B7": b7_pass,
    "B9": b9_pass,
    "A2": a2_pass,
    "C10": c10_pass,
    "C11": c11_pass,
}
for check, passed in results.items():
    print(f"  {check}: {'PASS' if passed else 'FAIL'}")

all_pass = all(results.values())
print(f"\nOverall: {'ALL PASS' if all_pass else 'SOME FAIL'}")
sys.exit(0 if all_pass else 1)
