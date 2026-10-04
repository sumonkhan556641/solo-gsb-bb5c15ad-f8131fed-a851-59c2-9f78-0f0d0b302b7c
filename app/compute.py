"""时间窗推算引擎。

输入：事件（起始时间范围 + 持续分钟数）与“先于”关系。
“A 先于 B”表示 A 结束后 B 才能开始；关系可携带间隔设置：
  min_gap_minutes  最短等待（非负整数分钟，缺省 0）
  max_gap_minutes  最长等待（非负整数分钟，可空 = 不限）
即  A 结束 + 最短等待 <= B 开始 <= A 结束 + 最长等待。
未设置间隔的旧关系等价于 最短等待=0、最长等待不限，也就是旧语义的
“A 开始 + A 持续 <= B 开始”，推算结果与升级前完全一致。

输出：每个完整事件的可行开始时间窗（最早/最晚可行开始）及推算依据链；
约束冲突（窗口不可满足、关系成环）时给出冲突事件、冲突类型与双方依据链；
缺少时间范围的事件作为“暂存”处理，其关联关系不参与推算并标明原因。
"""

from datetime import datetime, timedelta


def _parse(s):
    return datetime.fromisoformat(s)


def _fmt(dt):
    return dt.isoformat(timespec="minutes")


def _gap_min(r):
    """最短等待分钟数；未设置的旧关系按 0（前项结束不晚于后项开始）。"""
    g = r.get("min_gap_minutes")
    return 0 if g is None else int(g)


def _gap_max(r):
    """最长等待分钟数；None 表示不限。"""
    m = r.get("max_gap_minutes")
    return None if m is None else int(m)


def compute(events, relations):
    """推算时间窗。

    events:   [{"id", "name", "earliest_start", "latest_start", "duration_minutes"}]
              时间字段为 ISO 字符串或 None；duration_minutes 为整数或 None。
    relations: [{"id", "predecessor_id", "successor_id",
                 "min_gap_minutes"?, "max_gap_minutes"?}]
              间隔字段缺省表示未设置：最短等待按 0、最长等待不限（旧行为）。

    返回 dict：
      status            "ok" | "conflict"
      windows           {event_id: {earliest_start, latest_start, earliest_end,
                                    latest_end, earliest_chain, latest_chain}}
      conflicts         [{type, event_ids, message, ...}]
      drafts            [{id, name, reason}]            暂存事件（不参与推算）
      skipped_relations [{id, predecessor_id, successor_id, reason, ...}]
    """
    by_id = {e["id"]: e for e in events}

    # ---- 1. 区分完整事件与暂存事件 -------------------------------------
    drafts = []
    active = {}
    for e in events:
        missing = []
        if not e.get("earliest_start"):
            missing.append("最早开始时间")
        if not e.get("latest_start"):
            missing.append("最晚开始时间")
        if e.get("duration_minutes") is None:
            missing.append("持续分钟数")
        if missing:
            drafts.append({
                "id": e["id"],
                "name": e["name"],
                "reason": "缺少" + "、".join(missing) + "，已暂存，不参与本次推算",
            })
        else:
            active[e["id"]] = e

    # ---- 2. 过滤涉及暂存事件的关系 --------------------------------------
    skipped_relations = []
    active_relations = []
    for r in relations:
        a, b = r["predecessor_id"], r["successor_id"]
        inactive = [by_id[x]["name"] for x in (a, b) if x in by_id and x not in active]
        if a not in by_id or b not in by_id:
            skipped_relations.append({
                "id": r["id"], "predecessor_id": a, "successor_id": b,
                "min_gap_minutes": _gap_min(r), "max_gap_minutes": _gap_max(r),
                "reason": "关系引用了不存在的事件，已忽略",
            })
        elif inactive:
            skipped_relations.append({
                "id": r["id"], "predecessor_id": a, "successor_id": b,
                "min_gap_minutes": _gap_min(r), "max_gap_minutes": _gap_max(r),
                "reason": "关联事件「" + "、".join(inactive) + "」处于暂存状态，该“先于”关系暂不参与推算",
            })
        else:
            active_relations.append(r)

    est = {i: _parse(e["earliest_start"]) for i, e in active.items()}
    lst = {i: _parse(e["latest_start"]) for i, e in active.items()}
    dur = {i: int(e["duration_minutes"]) for i, e in active.items()}
    # 每条生效关系的间隔设置：(最短等待, 最长等待|None)
    rel_gap = {r["id"]: (_gap_min(r), _gap_max(r)) for r in active_relations}
    # 依据来源：est_src[b] = (a, relation_id, edge)，edge 为 "min"（最短等待边，
    # 沿关系方向）或 "max"（最长等待边，逆关系方向）
    est_src = {i: None for i in active}
    lst_src = {i: None for i in active}

    conflicts = []
    invalid_range = set()
    for i, e in active.items():
        if est[i] > lst[i]:
            invalid_range.add(i)
            conflicts.append({
                "type": "invalid_range",
                "event_ids": [i],
                "message": "事件「{}」输入的时间范围自相矛盾：最早开始 {} 晚于最晚开始 {}".format(
                    e["name"], _fmt(est[i]), _fmt(lst[i])),
            })

    # ---- 3. 前向/后向松弛（Bellman-Ford 方式，检测循环） -----------------
    # 每条关系 A→B（最短等待 g、最长等待 m）展开为四条差分约束：
    #   est[B] >= est[A] + dur[A] + g      （A 结束 + 最短等待 才能到 B）
    #   est[A] >= est[B] - dur[A] - m      （B 不得晚于 A 结束 + 最长等待）
    #   lst[A] <= lst[B] - dur[A] - g
    #   lst[B] <= lst[A] + dur[A] + m
    n = len(active)

    def relax_forward():
        changed = False
        for r in active_relations:
            a, b = r["predecessor_id"], r["successor_id"]
            g, m = rel_gap[r["id"]]
            cand = est[a] + timedelta(minutes=dur[a] + g)
            if cand > est[b]:
                est[b] = cand
                est_src[b] = (a, r["id"], "min")
                changed = True
            if m is not None:
                cand = est[b] - timedelta(minutes=dur[a] + m)
                if cand > est[a]:
                    est[a] = cand
                    est_src[a] = (b, r["id"], "max")
                    changed = True
        return changed

    def relax_backward():
        changed = False
        for r in active_relations:
            a, b = r["predecessor_id"], r["successor_id"]
            g, m = rel_gap[r["id"]]
            cand = lst[b] - timedelta(minutes=dur[a] + g)
            if cand < lst[a]:
                lst[a] = cand
                lst_src[a] = (b, r["id"], "min")
                changed = True
            if m is not None:
                cand = lst[a] + timedelta(minutes=dur[a] + m)
                if cand < lst[b]:
                    lst[b] = cand
                    lst_src[b] = (a, r["id"], "max")
                    changed = True
        return changed

    forward_cycle = backward_cycle = False
    if n:
        for _ in range(n):
            if not relax_forward():
                break
        else:
            forward_cycle = True
        for _ in range(n):
            if not relax_backward():
                break
        else:
            backward_cycle = True

    def find_cycle(src):
        for start in active:
            seen = {}
            path = []
            cur = start
            while cur is not None and cur not in seen:
                seen[cur] = len(path)
                path.append(cur)
                s = src.get(cur)
                cur = s[0] if s else None
            if cur is not None:
                return path[seen[cur]:]
        return []

    # ---- 4. 依据链构建 ---------------------------------------------------
    def gap_text(g, m):
        parts = []
        if g:
            parts.append("最短等待 {} 分钟".format(g))
        if m is not None:
            parts.append("最长等待 {} 分钟".format(m))
        return "、".join(parts)

    def build_chain(start_id, kind):
        """从结论回溯到输入依据，返回按“输入 → 结论”排列的步骤列表。"""
        steps = []
        cur = start_id
        seen = set()
        while True:
            ev = active[cur]
            src = est_src[cur] if kind == "est" else lst_src[cur]
            val = est[cur] if kind == "est" else lst[cur]
            if src is None:
                label = "最早开始" if kind == "est" else "最晚开始"
                steps.append({"text": "「{}」输入的{}为 {}".format(ev["name"], label, _fmt(val))})
                break
            other, rid, edge = src
            oev = active[other]
            g, m = rel_gap.get(rid, (0, None))
            if kind == "est":
                if edge == "min":
                    # other 先于 cur：cur 最早开始 = other 最早开始 + other 持续 + 最短等待
                    text = "「{}」先于「{}」且「{}」持续 {} 分钟".format(
                        oev["name"], ev["name"], oev["name"], dur[other])
                    gaps = gap_text(g, None)
                    if gaps:
                        text += "、" + gaps
                    text += " ⇒ 「{}」最早开始不早于 {}".format(ev["name"], _fmt(val))
                else:
                    # cur 先于 other 且设最长等待：cur 最早开始 = other 最早开始 - cur 持续 - 最长等待
                    text = "「{}」先于「{}」且最长等待 {} 分钟（「{}」持续 {} 分钟）⇒ 「{}」最早开始不早于 {}".format(
                        ev["name"], oev["name"], m, ev["name"], dur[cur], ev["name"], _fmt(val))
            else:
                if edge == "min":
                    # cur 先于 other：cur 最晚开始 = other 最晚开始 - cur 持续 - 最短等待
                    text = "「{}」先于「{}」且「{}」持续 {} 分钟".format(
                        ev["name"], oev["name"], ev["name"], dur[cur])
                    gaps = gap_text(g, None)
                    if gaps:
                        text += "、" + gaps
                    text += " ⇒ 「{}」最晚开始不晚于 {}".format(ev["name"], _fmt(val))
                else:
                    # other 先于 cur 且设最长等待：cur 最晚开始 = other 最晚开始 + other 持续 + 最长等待
                    text = "「{}」先于「{}」且最长等待 {} 分钟（「{}」持续 {} 分钟）⇒ 「{}」最晚开始不晚于 {}".format(
                        oev["name"], ev["name"], m, oev["name"], dur[other], ev["name"], _fmt(val))
            steps.append({"text": text})
            if cur in seen:
                steps.append({"text": "（检测到循环依赖，回溯终止）"})
                break
            seen.add(cur)
            cur = other
        steps.reverse()
        return steps

    if forward_cycle or backward_cycle:
        cyc = find_cycle(est_src) or find_cycle(lst_src)
        names = [active[i]["name"] for i in cyc]
        conflict = {
            "type": "cycle",
            "event_ids": cyc,
            "message": "“先于”关系构成循环：{}，约束不可满足".format(
                " → ".join(names + names[:1]) if names else "未知"),
        }
        if cyc:
            # 给出环上事件的双方依据链（正推/反推），展示相互矛盾的推导
            conflict["earliest_chain"] = build_chain(cyc[0], "est")
            conflict["latest_chain"] = build_chain(cyc[0], "lst")
        conflicts.append(conflict)

    # ---- 5. 汇总每个事件的可行时间窗 ------------------------------------
    windows = {}
    for i in active:
        windows[i] = {
            "earliest_start": _fmt(est[i]),
            "latest_start": _fmt(lst[i]),
            "earliest_end": _fmt(est[i] + timedelta(minutes=dur[i])),
            "latest_end": _fmt(lst[i] + timedelta(minutes=dur[i])),
            "earliest_chain": build_chain(i, "est"),
            "latest_chain": build_chain(i, "lst"),
        }

    # ---- 6. 窗口冲突（最早可行开始晚于最晚可行开始） ----------------------
    if not (forward_cycle or backward_cycle):
        for i in active:
            if i in invalid_range:
                continue  # 已在输入层面报告
            if est[i] > lst[i]:
                conflicts.append({
                    "type": "window",
                    "event_ids": [i],
                    "message": "事件「{}」推算后最早可行开始 {} 晚于最晚可行开始 {}，约束冲突".format(
                        active[i]["name"], _fmt(est[i]), _fmt(lst[i])),
                    "earliest_chain": windows[i]["earliest_chain"],
                    "latest_chain": windows[i]["latest_chain"],
                })

    return {
        "status": "conflict" if conflicts else "ok",
        "windows": windows,
        "conflicts": conflicts,
        "drafts": drafts,
        "skipped_relations": skipped_relations,
    }
