"""滚动切换窗口：阶段切分、组合核对、阻断停步、阶段执行状态机。"""
import pytest

from app.compat import judge
from app.matching import match_fields
from app.rollout import advance, build_plan, initial_execution


def f(fid, name, type_="string", nullable=True, default=None, partition=False):
    return {"id": fid, "name": name, "type": type_, "nullable": nullable, "default": default, "partition": partition}


def compat_for(old_fields, new_fields):
    return judge(match_fields(old_fields, new_fields)["pairs"])


def plan_for(old_fields, new_fields, tasks):
    return build_plan(tasks, compat_for(old_fields, new_fields))


def task(name, role, version, switch_at=None, start_at=None, stop_at=None):
    return {"name": name, "role": role, "version": version,
            "switch_at": switch_at, "start_at": start_at, "stop_at": stop_at, "columns": []}


FULL = ([f(1, "a")], [f(1, "a")])
WIDEN = ([f(1, "a", "int32", nullable=False)], [f(1, "a", "int64", nullable=False)])   # backward
NARROW = ([f(1, "a", "int64", nullable=False)], [f(1, "a", "int32", nullable=False)])  # forward


def test_no_tasks_means_unplanned():
    plan = build_plan([], compat_for(*FULL))
    assert plan["available"] is False
    assert plan["status"] == "unplanned"
    assert plan["stages"] == []
    assert "未提供任务清单" in plan["window_text"]


def test_stage_boundaries_follow_switch_times_and_combine_same_instant():
    tasks = [
        task("w", "writer", "old", "2026-01-01T02:00:00Z"),
        task("r1", "reader", "old", "2026-01-01T01:00:00Z"),
        task("r2", "reader", "old", "2026-01-01T01:00:00Z"),
    ]
    plan = plan_for(*FULL, tasks)
    assert plan["total_stages"] == 3
    assert plan["stages"][0]["at"] is None
    assert plan["stages"][1]["at"].startswith("2026-01-01T01:00:00")
    assert len(plan["stages"][1]["events"]) == 2      # 同一时刻的两条切换合并
    assert plan["stages"][2]["at"].startswith("2026-01-01T02:00:00")


def test_each_stage_checks_all_versions_active_in_window():
    tasks = [
        task("w_old", "writer", "old", "2026-01-01T03:00:00Z"),
        task("r_old", "reader", "old", "2026-01-01T04:00:00Z"),
        task("w_new", "writer", "new", start_at="2026-01-01T01:00:00Z"),
    ]
    plan = plan_for(*FULL, tasks)
    mid = plan["stages"][1]
    assert mid["writer_versions"] == ["old", "new"]
    assert mid["reader_versions"] == ["old"]
    combos = {(c["writer_version"], c["reader_version"]) for c in mid["checks"]}
    # 新旧写任务并行写：两个写入版本都要与该窗口的读取版本核对
    assert combos == {("old", "old"), ("new", "old")}


def test_backward_plan_stops_at_new_write_old_read():
    tasks = [
        task("r_old", "reader", "old"),
        task("w_old", "writer", "old", "2026-01-01T03:00:00Z"),
        task("r_new", "reader", "new", start_at="2026-01-01T01:00:00Z", stop_at="2026-01-01T05:00:00Z"),
    ]
    plan = plan_for(*WIDEN, tasks)
    assert plan["status"] == "blocked"
    assert plan["first_stop_stage"] == 3
    stage = plan["stages"][2]
    assert stage["status"] == "blocked"
    combo = stage["blocking_combos"][0]
    assert combo["writer_version"] == "new" and combo["reader_version"] == "old"
    assert combo["direction"] == "forward"
    assert plan["stages"][3]["status"] == "unreachable"
    assert plan["stages"][3]["checks"] == []
    assert plan["safe_stage_count"] == 2


def test_forward_plan_stops_at_old_write_new_read():
    tasks = [
        task("w_old", "writer", "old", "2026-01-01T03:00:00Z"),
        task("r_new", "reader", "new", start_at="2026-01-01T01:00:00Z"),
    ]
    plan = plan_for(*NARROW, tasks)
    assert plan["status"] == "blocked"
    assert plan["first_stop_stage"] == 2
    combo = plan["stages"][1]["blocking_combos"][0]
    assert combo["direction"] == "backward"


def test_stopped_tasks_leave_the_window():
    tasks = [
        task("r_old", "reader", "old", "2026-01-01T04:00:00Z", stop_at="2026-01-01T02:00:00Z"),
        task("w_old", "writer", "old", "2026-01-01T03:00:00Z"),
    ]
    plan = plan_for(*NARROW, tasks)
    assert plan["status"] == "ready"
    stage = plan["stages"][1]
    assert stage["active_readers"] == []


def test_unknown_combo_stops_plan_as_needs_confirm():
    unknown = ([f(1, "a", "timestamp", nullable=False)], [f(1, "a", "date", nullable=False)])
    tasks = [task("w_old", "writer", "old", "2026-01-01T02:00:00Z"),
             task("r_new", "reader", "new", start_at="2026-01-01T01:00:00Z")]
    plan = plan_for(*unknown, tasks)
    assert plan["status"] == "needs_confirm"
    assert plan["stages"][1]["status"] == "needs_confirm"


def test_parallel_old_and_new_writers_are_both_checked():
    """新旧生产任务并行写：窗口内两个写入版本都要与该阶段的读取版本核对。"""
    tasks = [
        task("w_old", "writer", "old", "2026-01-01T05:00:00Z"),
        task("w_new", "writer", "new", start_at="2026-01-01T01:00:00Z"),
        task("r_old", "reader", "old"),
    ]
    plan = plan_for(*WIDEN, tasks)
    stage = plan["stages"][1]
    assert stage["writer_versions"] == ["old", "new"]
    assert {c["writer_version"] for c in stage["checks"]} == {"old", "new"}
    assert plan["status"] == "blocked"
    assert plan["first_stop_stage"] == 2
    assert plan["blocking_combos"][0]["writer_tasks"] == ["w_new"]


def test_advance_rejects_out_of_order_and_bad_stage():
    plan = plan_for(*FULL, [task("w", "writer", "old", "2026-01-01T02:00:00Z")])
    execution = initial_execution(plan)
    state, result = advance(plan, execution, 2)
    assert result["result"] == "out_of_order"
    assert state["current_stage"] == 1
    assert state["completed_stages"] == []
    state, result = advance(plan, execution, 9)
    assert result["result"] == "invalid_stage"
    assert state["completed_stages"] == []


def test_advance_retry_failed_stage_keeps_state():
    tasks = [
        task("r_old", "reader", "old"),
        task("w_old", "writer", "old", "2026-01-01T03:00:00Z"),
        task("r_new", "reader", "new", start_at="2026-01-01T01:00:00Z", stop_at="2026-01-01T05:00:00Z"),
    ]
    plan = plan_for(*WIDEN, tasks)
    execution = initial_execution(plan)

    need = plan["first_stop_stage"] - 1
    for _ in range(need):
        execution, result = advance(plan, execution)
        assert result["ok"] is True

    before = dict(execution)
    execution, result = advance(plan, execution)
    assert result["ok"] is False
    assert result["result"] == "failed"
    assert execution["status"] == "blocked"
    assert execution["current_stage"] == before["current_stage"]
    assert execution["completed_stages"] == before["completed_stages"]
    assert execution["attempts"][str(need + 1)] == 1

    execution, result = advance(plan, execution)
    assert result["ok"] is False
    assert execution["attempts"][str(need + 1)] == 2
    assert execution["completed_stages"] == before["completed_stages"]
    assert execution["current_stage"] == before["current_stage"]


def test_retry_completed_stage_does_not_advance_twice():
    plan = plan_for(*FULL, [task("w", "writer", "old", "2026-01-01T02:00:00Z")])
    execution = initial_execution(plan)
    execution, result = advance(plan, execution)
    assert result["result"] == "advanced"
    assert execution["completed_stages"] == [1]
    assert execution["current_stage"] == 2

    execution, result = advance(plan, execution, 1)
    assert result["result"] == "already_completed"
    assert execution["completed_stages"] == [1]
    assert execution["current_stage"] == 2


def test_all_stages_completed_marks_completed_and_retry_is_noop():
    plan = plan_for(*FULL, [task("w", "writer", "old", "2026-01-01T02:00:00Z")])
    execution = initial_execution(plan)
    for _ in range(plan["total_stages"]):
        execution, result = advance(plan, execution)
        assert result["ok"] is True
    assert execution["status"] == "completed"
    assert execution["current_stage"] is None
    assert execution["completed_stages"] == [1, 2]
    execution, result = advance(plan, execution)
    assert result["result"] == "already_completed"
    assert execution["completed_stages"] == [1, 2]


def test_stage_checks_are_immutable_when_advanced_past_blocked_stage():
    tasks = [
        task("r_old", "reader", "old"),
        task("w_old", "writer", "old", "2026-01-01T03:00:00Z"),
        task("r_new", "reader", "new", start_at="2026-01-01T01:00:00Z", stop_at="2026-01-01T05:00:00Z"),
    ]
    plan = plan_for(*WIDEN, tasks)
    execution = initial_execution(plan)
    for _ in range(10):
        execution, _ = advance(plan, execution)
    assert len(execution["completed_stages"]) == plan["first_stop_stage"] - 1
    assert execution["status"] == "blocked"
