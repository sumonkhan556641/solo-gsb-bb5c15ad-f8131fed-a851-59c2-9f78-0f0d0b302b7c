"""推算引擎单元测试：python tests/test_compute.py"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import compute  # noqa: E402


def ev(i, name, es, ls, dur):
    return {"id": i, "name": name, "earliest_start": es, "latest_start": ls,
            "duration_minutes": dur}


def rel(i, a, b):
    return {"id": i, "predecessor_id": a, "successor_id": b}


def rel_gap(i, a, b, min_g=None, max_g=None):
    r = {"id": i, "predecessor_id": a, "successor_id": b}
    if min_g is not None:
        r["min_gap_minutes"] = min_g
    if max_g is not None:
        r["max_gap_minutes"] = max_g
    return r


def test_basic_propagation():
    events = [
        ev(1, "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
        ev(2, "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ]
    res = compute.compute(events, [rel(1, 1, 2)])
    assert res["status"] == "ok", res
    w2 = res["windows"][2]
    # 响应最早开始 = 告警最早开始 10:00 + 15 分钟
    assert w2["earliest_start"] == "2026-10-01T10:15", w2
    w1 = res["windows"][1]
    # 告警最晚开始 = 响应最晚开始 12:00 - 15 分钟（输入 10:30 更紧，保持 10:30）
    assert w1["latest_start"] == "2026-10-01T10:30", w1
    # 依据链应包含输入与关系两个环节
    assert len(w2["earliest_chain"]) == 2
    assert "告警" in w2["earliest_chain"][0]["text"]


def test_backward_propagation():
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T23:00", 60),
        ev(2, "B", "2026-10-01T10:00", "2026-10-01T11:00", 10),
    ]
    res = compute.compute(events, [rel(1, 1, 2)])
    assert res["status"] == "ok", res
    # A 最晚开始 = B 最晚开始 11:00 - 60 分钟 = 10:00
    assert res["windows"][1]["latest_start"] == "2026-10-01T10:00"


def test_window_conflict():
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T10:10", 60),
        ev(2, "B", "2026-10-01T10:00", "2026-10-01T10:30", 5),
    ]
    res = compute.compute(events, [rel(1, 1, 2)])
    assert res["status"] == "conflict"
    win_conflicts = [c for c in res["conflicts"] if c["type"] == "window"]
    # A 的最晚可行开始被回推到 09:30（< 最早 10:00），B 的最早可行开始 11:00 > 最晚 10:30
    assert {c["event_ids"][0] for c in win_conflicts} == {1, 2}
    assert res["windows"][2]["earliest_start"] == "2026-10-01T11:00"
    c2 = next(c for c in win_conflicts if c["event_ids"] == [2])
    assert c2["earliest_chain"] and c2["latest_chain"]


def test_cycle_conflict():
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T12:00", 30),
        ev(2, "B", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ]
    res = compute.compute(events, [rel(1, 1, 2), rel(2, 2, 1)])
    assert res["status"] == "conflict"
    assert any(c["type"] == "cycle" for c in res["conflicts"])


def test_invalid_input_range():
    events = [ev(1, "A", "2026-10-01T11:00", "2026-10-01T10:00", 10)]
    res = compute.compute(events, [])
    assert res["status"] == "conflict"
    assert res["conflicts"][0]["type"] == "invalid_range"


def test_draft_event_skips_relation():
    events = [
        ev(1, "完整", "2026-10-01T10:00", "2026-10-01T11:00", 10),
        ev(2, "缺范围", None, None, 10),
        ev(3, "缺时长", "2026-10-01T10:00", "2026-10-01T11:00", None),
    ]
    res = compute.compute(events, [rel(1, 1, 2), rel(2, 2, 3)])
    assert res["status"] == "ok"
    assert len(res["drafts"]) == 2
    assert len(res["skipped_relations"]) == 2
    assert set(res["windows"].keys()) == {1}
    assert "暂存" in res["drafts"][0]["reason"]


def test_empty():
    res = compute.compute([], [])
    assert res["status"] == "ok" and res["windows"] == {}


def test_min_gap_pushes_earliest():
    # 最短等待 45 分钟：B 最早开始 = A 最早开始 + A 持续 + 45
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T10:30", 15),
        ev(2, "B", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ]
    res = compute.compute(events, [rel_gap(1, 1, 2, min_g=45)])
    assert res["status"] == "ok", res
    assert res["windows"][2]["earliest_start"] == "2026-10-01T11:00"
    # A 最晚开始 = B 最晚开始 12:00 - 15 - 45 = 11:00，但输入 10:30 更紧
    assert res["windows"][1]["latest_start"] == "2026-10-01T10:30"
    text = res["windows"][2]["earliest_chain"][-1]["text"]
    assert "最短等待 45 分钟" in text


def test_max_gap_bounds_successor_latest():
    # 最长等待 20 分钟：B 最晚开始 = A 最晚开始 + A 持续 + 20
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T10:30", 15),
        ev(2, "B", "2026-10-01T10:00", "2026-10-01T23:00", 30),
    ]
    res = compute.compute(events, [rel_gap(1, 1, 2, max_g=20)])
    assert res["status"] == "ok", res
    assert res["windows"][2]["latest_start"] == "2026-10-01T11:05"
    text = res["windows"][2]["latest_chain"][-1]["text"]
    assert "最长等待 20 分钟" in text


def test_max_gap_pushes_predecessor_earliest():
    # 最长等待反向约束前项：A 最早开始 = B 最早开始 - A 持续 - 最长等待
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T23:00", 15),
        ev(2, "B", "2026-10-01T12:00", "2026-10-01T13:00", 30),
    ]
    res = compute.compute(events, [rel_gap(1, 1, 2, max_g=20)])
    assert res["status"] == "ok", res
    assert res["windows"][1]["earliest_start"] == "2026-10-01T11:25"
    text = res["windows"][1]["earliest_chain"][-1]["text"]
    assert "最长等待 20 分钟" in text


def test_min_max_together():
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T10:30", 15),
        ev(2, "B", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ]
    res = compute.compute(events, [rel_gap(1, 1, 2, min_g=10, max_g=60)])
    assert res["status"] == "ok", res
    w2 = res["windows"][2]
    assert w2["earliest_start"] == "2026-10-01T10:25"   # 10:00 + 15 + 10
    assert w2["latest_start"] == "2026-10-01T11:45"     # 10:30 + 15 + 60
    w1 = res["windows"][1]
    assert w1["latest_start"] == "2026-10-01T10:30"     # 12:00 - 15 - 10 = 11:35，输入更紧
    assert w1["earliest_start"] == "2026-10-01T10:00"   # 10:00 - 15 - 60 更早，输入更紧


def test_max_gap_conflict_opposing_chains():
    # B 输入最早 12:00，但 A 最晚 10:00 结束 10:30 + 最长等待 10 ⇒ B 最晚 10:40，冲突
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T10:00", 30),
        ev(2, "B", "2026-10-01T12:00", "2026-10-01T13:00", 30),
    ]
    res = compute.compute(events, [rel_gap(1, 1, 2, max_g=10)])
    assert res["status"] == "conflict"
    win = {c["event_ids"][0]: c for c in res["conflicts"] if c["type"] == "window"}
    assert set(win) == {1, 2}
    # 冲突事件给出相反依据链（最早/最晚两条），且链中出现最长等待推导
    assert win[1]["earliest_chain"] and win[1]["latest_chain"]
    assert win[2]["earliest_chain"] and win[2]["latest_chain"]
    all_text = " ".join(
        s["text"] for c in win.values() for ch in (c["earliest_chain"], c["latest_chain"])
        for s in ch)
    assert "最长等待 10 分钟" in all_text


def test_cycle_with_gaps_has_opposing_chains():
    events = [
        ev(1, "A", "2026-10-01T10:00", "2026-10-01T12:00", 30),
        ev(2, "B", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ]
    res = compute.compute(events, [rel_gap(1, 1, 2, min_g=5), rel_gap(2, 2, 1, max_g=90)])
    assert res["status"] == "conflict"
    cyc = [c for c in res["conflicts"] if c["type"] == "cycle"]
    assert cyc, res["conflicts"]
    assert set(cyc[0]["event_ids"]) == {1, 2}
    assert cyc[0]["earliest_chain"] and cyc[0]["latest_chain"]


def test_old_relation_without_gaps_unchanged():
    # 未设置间隔的旧关系：推算与依据链文案保持旧行为
    events = [
        ev(1, "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
        ev(2, "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30),
    ]
    res = compute.compute(events, [rel(1, 1, 2)])
    assert res["status"] == "ok"
    assert res["windows"][2]["earliest_start"] == "2026-10-01T10:15"
    text = res["windows"][2]["earliest_chain"][-1]["text"]
    assert "持续 15 分钟" in text and "等待" not in text


def test_skipped_relation_keeps_gap_info():
    events = [
        ev(1, "完整", "2026-10-01T10:00", "2026-10-01T11:00", 10),
        ev(2, "缺范围", None, None, 10),
    ]
    res = compute.compute(events, [rel_gap(1, 1, 2, min_g=5, max_g=30)])
    assert res["status"] == "ok"
    assert len(res["skipped_relations"]) == 1
    s = res["skipped_relations"][0]
    assert s["min_gap_minutes"] == 5 and s["max_gap_minutes"] == 30
    assert "暂存" in s["reason"]


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", t.__name__, "->", e)
    sys.exit(1 if failed else 0)
