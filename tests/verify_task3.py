"""
Verification script for Task 3 acceptance criteria (B5-B10).
Run with: uv run python tests/verify_task3.py
Uses a temporary isolated SQLite DB so it doesn't pollute data/xhs_op.db.
"""
from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path

# Must be run with XHS_COMPETITOR_STUB=1
os.environ["XHS_COMPETITOR_STUB"] = "1"

results: list[tuple[str, bool, str]] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    results.append((label, passed, detail))
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {label}: {detail}" if detail else f"[{status}] {label}")


# ---- B8: Jaccard function ---------------------------------------------------
from xhs_op.generate.inspirer import jaccard

s1 = jaccard("推开木门是一整片雨林，清晨被鸟叫声唤醒", "推开木门是一整片雨林，清晨被鸟叫声唤醒")
s2 = jaccard("完全不同的一段文字，没有任何重复", "另一段完全不同的描述，无重叠字符")
s3 = jaccard("这是一段测试文字", "")

check("B8a: jaccard identical == 1.0", abs(s1 - 1.0) < 1e-9, f"got {s1}")
check("B8b: jaccard dissimilar < 0.05", s2 < 0.05, f"got {s2:.4f}")
check("B8c: jaccard with empty == 0.0", abs(s3 - 0.0) < 1e-9, f"got {s3}")

# ---- B9: PlagiarismGuardFailed ---------------------------------------------
from xhs_op.generate.inspirer import PlagiarismGuardFailed

e = PlagiarismGuardFailed(42, 0.22)
e_str = str(e)
check("B9a: PlagiarismGuardFailed str contains idea_id", "42" in e_str, e_str)
check("B9b: PlagiarismGuardFailed idea_id attr", e.idea_id == 42, f"got {e.idea_id}")
check("B9c: PlagiarismGuardFailed score attr", abs(e.score - 0.22) < 0.001, f"got {e.score}")
print("PlagiarismGuardFailed OK")

# ---- B10: _extract_skeleton ------------------------------------------------
from xhs_op.generate.inspirer import _extract_skeleton
from xhs_op.db import Idea

# Create a fake Idea-like object using direct attribute assignment
import types
fake_idea = types.SimpleNamespace(
    id=None,
    source="xhs_competitor",
    source_url="https://example.com/fake",
    source_id="fake_001",
    raw_title="傣家小院",
    raw_body="推开木门是一整片雨林，清晨被鸟叫声唤醒。院子里摆着藤椅和小桌。",
    raw_lang="zh",
    engagement_score=50.0,
    category="banna_villa",
    target_account="banna",
    extra={"hashtags": ["慢生活", "雨林"], "image_urls": ["a.jpg", "b.jpg", "c.jpg"]},
    processed=False,
)

skeleton = _extract_skeleton(fake_idea)
print(f"skeleton hook_pattern: {skeleton.get('hook_pattern')!r}")
print(f"skeleton body_shape: {skeleton.get('body_shape')}")
print(f"skeleton image_layout_archetype: {skeleton.get('image_layout_archetype')!r}")

hook = skeleton.get("hook_pattern", "")
check("B10a: hook_pattern non-empty", bool(hook), f"got {hook!r}")
check("B10b: hook_pattern starts with first sentence", hook.startswith("推开木门是一整片雨林"), f"got {hook!r}")

body_shape = skeleton.get("body_shape", {})
check("B10c: body_shape paragraph_count >= 1", body_shape.get("paragraph_count", 0) >= 1, f"paragraph_count={body_shape.get('paragraph_count')}")
check("B10d: body_shape total_chars > 0", body_shape.get("total_chars", 0) > 0, f"total_chars={body_shape.get('total_chars')}")

arch = skeleton.get("image_layout_archetype")
check("B10e: image_layout_archetype == 'grid' for 3 images", arch == "grid", f"got {arch!r}")

# ---- B5 + B6 + B7: stub fetch and DB query (isolated temp DB) --------------
# Temporarily override the DB engine to use an isolated temp file
import xhs_op.db as _db_mod
from sqlmodel import create_engine as _create_engine, Session, select as _select

_tmpdir = tempfile.mkdtemp()
_tmp_db = Path(_tmpdir) / "test_task3.db"
_tmp_url = f"sqlite:///{_tmp_db.as_posix()}"
_old_engine = _db_mod.engine

_db_mod.engine = _create_engine(_tmp_url, connect_args={"check_same_thread": False})
from sqlmodel import SQLModel as _SQLModel
_SQLModel.metadata.create_all(_db_mod.engine)

try:
    from xhs_op.sources import xhs_competitor as _xc_mod
    # Also patch the engine reference used in get_session
    import xhs_op.db as _db2
    _db2.engine = _db_mod.engine

    # B5: banna_villa keyword
    print("\nRunning stub fetch for 西双版纳民宿...")
    n5 = _xc_mod.fetch(only_keyword="西双版纳民宿", limit=50)
    print(f"  inserted: {n5}")

    with Session(_db_mod.engine) as session:
        banna_rows = session.exec(
            _select(Idea).where(Idea.source == "xhs_competitor", Idea.category == "banna_villa")
        ).all()
        banna_data = [(r.source_id, r.category, r.target_account) for r in banna_rows]

    banna_count = len(banna_data)
    banna_categories = set(c for _, c, _ in banna_data)
    banna_targets_set = set(t for _, _, t in banna_data)

    check("B5a: banna_villa rows exist", banna_count > 0, f"count={banna_count}")
    check("B5b: all rows have category=banna_villa", banna_categories == {"banna_villa"}, str(banna_categories))
    check("B5c: all rows have target_account=banna", banna_targets_set == {"banna"}, str(banna_targets_set))
    print(f"  Note IDs: {[sid for sid, _, _ in banna_data]}")

    # B6: luxury_hotel keyword
    print("\nRunning stub fetch for 奢华酒店...")
    n6 = _xc_mod.fetch(only_keyword="奢华酒店", limit=50)
    print(f"  inserted: {n6}")

    with Session(_db_mod.engine) as session:
        lux_rows = session.exec(
            _select(Idea).where(Idea.source == "xhs_competitor", Idea.category == "luxury_hotel")
        ).all()
        lux_data = [(r.source_id, r.category, r.target_account) for r in lux_rows]

    lux_count = len(lux_data)
    lux_categories = set(c for _, c, _ in lux_data)

    check("B6a: luxury_hotel rows exist", lux_count > 0, f"count={lux_count}")
    check("B6b: all rows have category=luxury_hotel", lux_categories == {"luxury_hotel"}, str(lux_categories))
    print(f"  Note IDs: {[sid for sid, _, _ in lux_data]}")

    # B7: Dedupe — run banna_villa fetch again, count must not increase
    with Session(_db_mod.engine) as session:
        count_before = len(session.exec(
            _select(Idea).where(Idea.source == "xhs_competitor", Idea.category == "banna_villa")
        ).all())

    print("\nRunning stub fetch again for 西双版纳民宿 (dedupe test)...")
    n7 = _xc_mod.fetch(only_keyword="西双版纳民宿", limit=50)

    with Session(_db_mod.engine) as session:
        count_after = len(session.exec(
            _select(Idea).where(Idea.source == "xhs_competitor", Idea.category == "banna_villa")
        ).all())

    check("B7: dedupe — row count did not increase", count_before == count_after,
          f"before={count_before} after={count_after} inserted_on_retry={n7}")

finally:
    # Restore original engine
    _db_mod.engine = _old_engine
    _db2.engine = _old_engine

# ---- Summary ---------------------------------------------------------------
print("\n" + "=" * 60)
passed = [l for l, ok, _ in results if ok]
failed_list = [(l, d) for l, ok, d in results if not ok]
print(f"Passed: {len(passed)}/{len(results)}")
if failed_list:
    print("FAILED checks:")
    for l, d in failed_list:
        print(f"  - {l}: {d}")
    sys.exit(1)
else:
    print("All checks PASSED")
    sys.exit(0)
