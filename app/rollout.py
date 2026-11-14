"""滚动切换窗口（纯函数，无 IO）。

输入是任务清单：每个任务有当前版本（old/new）、读写类型（writer/reader）与切换时刻
（switch_at，可选 start_at / stop_at）。按时间切出阶段，每个阶段核对“该窗口内仍在运行
的所有写入版本 × 读取版本”：

- old 写 × new 读 → 需要 backward 兼容（旧数据 → 新 schema）
- new 写 × old 读 → 需要 forward 兼容（新数据 → 旧 schema）
- 同版本读写 → 无需跨版本判定

发现 blocked / unknown 组合即停在该阶段，后续阶段标记为未推进（unreachable）。
执行状态是独立的有限状态机：失败的阶段保留原状态，重试同一阶段不会重复推进。
"""
from __future__ import annotations

from datetime import datetime, timezone

ROLE_LABELS = {"writer": "写入方（生产任务）", "reader": "读取方（查询任务）"}
VERSION_LABELS = {"old": "旧版本", "new": "新版本"}
VERSION_ORDER = {"old": 0, "new": 1}

STAGE_STATUS_LABELS = {
    "safe": "安全",
    "blocked": "阻断",
    "needs_confirm": "需人工确认",
    "unreachable": "未推进（前序阶段阻断）",
}
PLAN_STATUS_LABELS = {
    "unplanned": "未提供任务清单",
    "ready": "全部阶段安全",
    "blocked": "存在不兼容组合",
    "needs_confirm": "存在待确认组合",
}
EXEC_STATUS_LABELS = {
    "unplanned": "无切换计划",
    "pending": "待执行",
    "in_progress": "进行中",
    "completed": "已完成全部阶段",
    "blocked": "阻断，停留在当前阶段",
    "needs_confirm": "需人工确认，停留在当前阶段",
}
VERDICT_LABELS = {"compatible": "兼容", "blocked": "不兼容", "unknown": "无法确认"}

ASSUMPTIONS = [
    "以任务清单的 switch_at 作为版本切换时刻；同一时刻的多条变更合并为一个阶段。",
    "version=new 表示任务已经在新版本上运行；新版本任务的启用时刻用 start_at 表达，switch_at 只用于 old→new 切换。",
    "start_at / stop_at 可选：任务在 start_at 前、stop_at 起不在运行；停掉的任务不参与后续窗口。",
    "阶段核对只比较该窗口内仍在运行的写入版本与读取版本，不代替真实发布操作。",
    "old 写 × new 读按 backward（旧写新读）判定；new 写 × old 读按 forward（新写旧读）判定。",
    "blocked 与 unknown 都无法保证安全窗口，均会让计划停在该阶段。",
]


def parse_time(value: str | None) -> datetime | None:
    """解析 ISO 8601 时间；无时区按 UTC 处理。非法值抛 ValueError。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _task_state(task: dict, at: datetime | None) -> dict | None:
    """任务在 at 时刻的状态；at=None 表示所有事件发生前的初始窗口。"""
    if at is None:
        # 初始窗口：已经运行的任务视为在跑；计划在未来才启动的任务不在跑。
        if task.get("start_at"):
            return None
        version = task["version"]
    else:
        start = parse_time(task.get("start_at"))
        if start is not None and at < start:
            return None
        stop = parse_time(task.get("stop_at"))
        if stop is not None and at >= stop:
            return None
        version = task["version"]
        if version == "old":
            switch = parse_time(task.get("switch_at"))
            if switch is not None and at >= switch:
                version = "new"
    return {
        "name": task["name"],
        "role": task["role"],
        "role_label": ROLE_LABELS[task["role"]],
        "version": version,
        "version_label": VERSION_LABELS[version],
        "columns": list(task.get("columns") or []),
    }


def _active_tasks(tasks: list[dict], at: datetime | None) -> list[dict]:
    rows = []
    for task in tasks:
        state = _task_state(task, at)
        if state is not None:
            rows.append(state)
    return rows


def _events(tasks: list[dict]) -> dict[datetime, list[dict]]:
    events: dict[datetime, list[dict]] = {}

    def add(at: datetime | None, task: dict, kind: str, text: str) -> None:
        if at is None:
            return
        events.setdefault(at, []).append({
            "task": task["name"],
            "role": task["role"],
            "role_label": ROLE_LABELS[task["role"]],
            "kind": kind,
            "text": text,
        })

    for task in tasks:
        add(parse_time(task.get("start_at")), task, "start",
            f"{task['name']} 开始运行（{VERSION_LABELS[task['version']]}）")
        if task["version"] == "old":
            add(parse_time(task.get("switch_at")), task, "switch",
                f"{task['name']} 切换到新版本")
        add(parse_time(task.get("stop_at")), task, "stop",
            f"{task['name']} 停止运行")
    for bucket in events.values():
        bucket.sort(key=lambda item: {"start": 0, "switch": 1, "stop": 2}[item["kind"]])
    return events


def _direction_label(compat: dict, direction: str) -> str:
    try:
        return compat["directions"][direction]["label"]
    except (KeyError, TypeError):
        return "旧写新读（旧数据 → 新 schema）" if direction == "backward" else "新写旧读（新数据 → 旧 schema）"


def _direction_reasons(compat: dict, direction: str) -> list[str]:
    rows = []
    seen = set()
    for item in compat.get("reasons", []):
        if item.get("direction") != direction or item.get("verdict") == "compatible":
            continue
        text = item.get("text")
        if text and text not in seen:
            seen.add(text)
            rows.append(text)
    return rows


def _combo_label(combo: dict) -> str:
    if combo.get("direction") == "backward":
        relation = "旧写新读（backward）"
    elif combo.get("direction") == "forward":
        relation = "新写旧读（forward）"
    else:
        relation = "同版本"
    return (f"写入 {VERSION_LABELS[combo['writer_version']]} × "
            f"读取 {VERSION_LABELS[combo['reader_version']]}（{relation}）")


def _make_stage(number: int, at: datetime | None, events: list[dict], active: list[dict],
                compat: dict) -> dict:
    writers = [item for item in active if item["role"] == "writer"]
    readers = [item for item in active if item["role"] == "reader"]
    checks: list[dict] = []
    blocking: list[dict] = []

    writer_versions = sorted({item["version"] for item in writers}, key=VERSION_ORDER.get)
    reader_versions = sorted({item["version"] for item in readers}, key=VERSION_ORDER.get)
    for writer_version in writer_versions:
        for reader_version in reader_versions:
            writer_tasks = [item["name"] for item in writers if item["version"] == writer_version]
            reader_tasks = [item["name"] for item in readers if item["version"] == reader_version]
            if writer_version == reader_version:
                verdict = "compatible"
                direction = None
                reason = f"写入与读取同为{VERSION_LABELS[writer_version]}，无需跨版本判定"
                reasons: list[str] = []
            else:
                direction = "backward" if writer_version == "old" else "forward"
                try:
                    verdict = compat["directions"][direction]["state"]
                except (KeyError, TypeError):
                    verdict = "unknown"
                reasons = _direction_reasons(compat, direction)
                reason = "；".join(reasons) if reasons else _direction_label(compat, direction)
            check = {
                "writer_version": writer_version,
                "writer_version_label": VERSION_LABELS[writer_version],
                "reader_version": reader_version,
                "reader_version_label": VERSION_LABELS[reader_version],
                "direction": direction,
                "direction_label": _direction_label(compat, direction) if direction else "同版本",
                "verdict": verdict,
                "verdict_label": VERDICT_LABELS[verdict],
                "compatible": verdict == "compatible",
                "writer_tasks": writer_tasks,
                "reader_tasks": reader_tasks,
                "reason": reason,
                "reasons": reasons,
            }
            check["label"] = _combo_label(check)
            checks.append(check)
            if verdict != "compatible":
                blocking.append({
                    "stage": number,
                    "writer_version": writer_version,
                    "reader_version": reader_version,
                    "direction": direction,
                    "direction_label": check["direction_label"],
                    "verdict": verdict,
                    "verdict_label": check["verdict_label"],
                    "label": check["label"],
                    "writer_tasks": writer_tasks,
                    "reader_tasks": reader_tasks,
                    "reason": reason,
                    "reasons": reasons,
                })

    if not writers or not readers:
        note = "窗口内没有可配对的写入方与读取方"
    elif not checks:
        note = "窗口内没有跨版本读写组合"
    else:
        note = "已核对窗口内全部写入版本 × 读取版本"
    if any(item["verdict"] == "blocked" for item in checks):
        status = "blocked"
    elif any(item["verdict"] == "unknown" for item in checks):
        status = "needs_confirm"
    else:
        status = "safe"

    if at is None:
        label = "初始窗口（首个事件前）"
    else:
        label = f"切换窗口 {at.astimezone(timezone.utc).isoformat(timespec='seconds')}"
    return {
        "stage": number,
        "label": label,
        "at": at.astimezone(timezone.utc).isoformat(timespec="seconds") if at is not None else None,
        "events": events,
        "active_writers": writers,
        "active_readers": readers,
        "writer_versions": writer_versions,
        "reader_versions": reader_versions,
        "checks": checks,
        "blocking_combos": blocking,
        "status": status,
        "status_label": STAGE_STATUS_LABELS[status],
        "checked": True,
        "note": note,
    }


def build_plan(tasks: list[dict], compat: dict) -> dict:
    """按任务切换时刻排阶段，逐阶段核对；首个阻断阶段之后不再推进。"""
    tasks = list(tasks or [])
    if not tasks:
        return {
            "available": False,
            "status": "unplanned",
            "status_label": PLAN_STATUS_LABELS["unplanned"],
            "stages": [],
            "total_stages": 0,
            "first_stop_stage": None,
            "safe_stage_count": 0,
            "blocking_combos": [],
            "window_text": "未提供任务清单，无法计算滚动切换安全窗口",
            "assumptions": ASSUMPTIONS,
        }

    events = _events(tasks)
    stages = [_make_stage(1, None, [], _active_tasks(tasks, None), compat)]
    for number, at in enumerate(sorted(events), start=2):
        stages.append(_make_stage(number, at, events[at], _active_tasks(tasks, at), compat))

    first_stop = None
    for stage in stages:
        if first_stop is not None:
            stage["checked"] = False
            stage["checks"] = []
            stage["blocking_combos"] = []
            stage["status"] = "unreachable"
            stage["status_label"] = STAGE_STATUS_LABELS["unreachable"]
            stage["note"] = f"阶段 {first_stop} 已存在阻断组合，按规则停在该阶段，本阶段未推进"
            continue
        if stage["status"] in ("blocked", "needs_confirm"):
            first_stop = stage["stage"]

    if first_stop is None:
        plan_status = "ready"
        blocking_combos: list[dict] = []
        safe_count = len(stages)
        window_text = f"全部 {len(stages)} 个阶段均在安全窗口内"
    else:
        stop_stage = stages[first_stop - 1]
        plan_status = stop_stage["status"]
        blocking_combos = stop_stage["blocking_combos"]
        safe_count = sum(1 for stage in stages if stage["status"] == "safe")
        if first_stop == 1:
            window_text = f"初始阶段即存在{PLAN_STATUS_LABELS[plan_status]}，没有可用的安全窗口"
        else:
            window_text = (f"阶段 1..{first_stop - 1} 为安全窗口；"
                           f"阶段 {first_stop} 起停止推进（{stop_stage['status_label']}）")

    return {
        "available": True,
        "status": plan_status,
        "status_label": PLAN_STATUS_LABELS[plan_status],
        "stages": stages,
        "total_stages": len(stages),
        "first_stop_stage": first_stop,
        "safe_stage_count": safe_count,
        "blocking_combos": blocking_combos,
        "window_text": window_text,
        "assumptions": ASSUMPTIONS,
    }


def initial_execution(plan: dict) -> dict:
    if not plan.get("available") or not plan.get("total_stages"):
        return {
            "status": "unplanned",
            "status_label": EXEC_STATUS_LABELS["unplanned"],
            "current_stage": None,
            "total_stages": plan.get("total_stages", 0),
            "completed_stages": [],
            "attempts": {},
            "last_attempt_at": None,
            "last_stage": None,
            "last_result": None,
            "last_error": None,
            "blocking_combos": [],
        }
    return {
        "status": "pending",
        "status_label": EXEC_STATUS_LABELS["pending"],
        "current_stage": 1,
        "total_stages": plan["total_stages"],
        "completed_stages": [],
        "attempts": {},
        "last_attempt_at": None,
        "last_stage": None,
        "last_result": None,
        "last_error": None,
        "blocking_combos": [],
    }


def advance(plan: dict, execution: dict, stage_id: int | None = None,
            now: str | None = None) -> tuple[dict, dict]:
    """推进/重试一个阶段。

    返回 (新执行状态, 结果)。失败的尝试只增加 attempts，current_stage 与
    completed_stages 保持原样；已完成的阶段再次提交直接返回 already_completed。
    """
    state = {
        **execution,
        "completed_stages": list(execution.get("completed_stages") or []),
        "attempts": dict(execution.get("attempts") or {}),
        "blocking_combos": list(execution.get("blocking_combos") or []),
    }
    total = int(plan.get("total_stages") or 0)
    if not plan.get("available") or total == 0:
        return state, {
            "ok": False,
            "result": "unplanned",
            "message": "该运行没有任务清单，无法执行切换阶段",
            "stage": None,
        }

    current = state.get("current_stage")
    requested = current if stage_id is None else stage_id
    if requested is None:
        return state, {
            "ok": True,
            "result": "already_completed",
            "message": f"全部 {total} 个阶段已完成，重试不会重复推进",
            "stage": None,
        }
    if isinstance(requested, bool) or not isinstance(requested, int):
        return state, {
            "ok": False,
            "result": "invalid_stage",
            "message": "stage 必须是整数",
            "stage": None,
        }
    if requested < 1 or requested > total:
        return state, {
            "ok": False,
            "result": "invalid_stage",
            "message": f"阶段 {requested} 不存在（有效范围 1..{total}）",
            "stage": requested,
        }
    if requested in state["completed_stages"]:
        return state, {
            "ok": True,
            "result": "already_completed",
            "message": f"阶段 {requested} 已完成，重试不会重复推进",
            "stage": requested,
        }
    if requested != current:
        return state, {
            "ok": False,
            "result": "out_of_order",
            "message": f"必须按顺序推进：当前阶段是 {current}，不能直接执行阶段 {requested}",
            "stage": requested,
            "expected_stage": current,
        }

    stage = plan["stages"][requested - 1]
    key = str(requested)
    attempted_at = now or _now_iso()
    state["attempts"][key] = int(state["attempts"].get(key, 0)) + 1
    state["last_attempt_at"] = attempted_at
    state["last_stage"] = requested

    if stage["status"] in ("blocked", "needs_confirm"):
        combos = stage["blocking_combos"]
        labels = "；".join(combo["label"] for combo in combos)
        state["status"] = stage["status"]
        state["status_label"] = EXEC_STATUS_LABELS[stage["status"]]
        state["last_result"] = "failed"
        state["last_error"] = f"阶段 {requested} 失败：{labels}"
        state["blocking_combos"] = combos
        return state, {
            "ok": False,
            "result": "failed",
            "message": (f"阶段 {requested} 失败：{labels}。已保留原状态"
                        f"（当前阶段 {requested}，已完成阶段 {_completed_text(state['completed_stages'])}），"
                        "修复后可重试本阶段。"),
            "stage": requested,
            "stage_status": stage["status"],
            "blocking_combos": combos,
        }

    if stage["status"] == "unreachable":
        state["last_result"] = "failed"
        state["last_error"] = f"阶段 {requested} 未推进：前序阶段已阻断"
        return state, {
            "ok": False,
            "result": "unreachable",
            "message": f"阶段 {requested} 不可执行：前序阶段已阻断，计划停在前序阶段",
            "stage": requested,
            "blocking_combos": plan.get("blocking_combos", []),
        }

    state["completed_stages"].append(requested)
    state["last_result"] = "advanced"
    state["last_error"] = None
    state["blocking_combos"] = []
    if requested >= total:
        state["current_stage"] = None
        state["status"] = "completed"
        state["status_label"] = EXEC_STATUS_LABELS["completed"]
    else:
        state["current_stage"] = requested + 1
        state["status"] = "in_progress"
        state["status_label"] = EXEC_STATUS_LABELS["in_progress"]
    return state, {
        "ok": True,
        "result": "advanced",
        "message": (f"阶段 {requested} 已通过。当前阶段 "
                    f"{state['current_stage'] if state['current_stage'] is not None else '已完成'}，"
                    f"已完成阶段 {_completed_text(state['completed_stages'])}。"),
        "stage": requested,
        "stage_status": stage["status"],
    }


def _completed_text(stages: list[int]) -> str:
    return "、".join(str(item) for item in stages) if stages else "无"


def execution_summary(plan: dict) -> dict:
    """给运行记录/历史列表用的扁平摘要。"""
    execution = plan.get("execution") or {}
    status = execution.get("status") or "unplanned"
    return {
        "rollout_status": status,
        "rollout_status_label": execution.get("status_label", EXEC_STATUS_LABELS.get(status, status)),
        "rollout_current_stage": execution.get("current_stage"),
        "rollout_total_stages": plan.get("total_stages", 0),
        "rollout_completed": len(execution.get("completed_stages") or []),
        "rollout_blocked_stage": plan.get("first_stop_stage"),
        "rollout_blocking_combos": len(plan.get("blocking_combos") or []),
    }
