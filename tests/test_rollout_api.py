"""API + 页面：滚动切换窗口、阶段推进与重试、历史/下载一致性。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def post_compare(payload):
    return client.post("/api/compare", json=payload)


def sample(key):
    return post_compare({"sample": key}).json()


def test_sample_a_rollout_all_stages_safe():
    body = sample("A")
    rollout = body["rollout"]
    assert rollout["available"] is True
    assert rollout["status"] == "ready"
    assert rollout["total_stages"] == 4
    assert body["summary"]["rollout_status"] == "pending"
    assert body["summary"]["rollout_total_stages"] == 4
    run = client.get(f"/api/runs/{body['run_id']}").json()
    assert run["rollout"]["status"] == "ready"


def test_sample_b_stops_at_new_write_old_read_stage():
    body = sample("B")
    rollout = body["rollout"]
    assert rollout["status"] == "blocked"
    assert rollout["first_stop_stage"] == 3
    combo = rollout["blocking_combos"][0]
    assert combo["label"] == "写入 新版本 × 读取 旧版本（新写旧读（forward））"
    assert combo["direction"] == "forward"
    assert combo["reader_tasks"] == ["reader_dashboard"]
    stage = rollout["stages"][2]
    assert stage["status"] == "blocked"
    assert rollout["stages"][3]["status"] == "unreachable"
    assert body["summary"]["rollout_blocking_combos"] == 1


def test_sample_c_stops_at_old_write_new_read_stage():
    body = sample("C")
    rollout = body["rollout"]
    assert rollout["status"] == "blocked"
    assert rollout["first_stop_stage"] == 2
    combo = rollout["blocking_combos"][0]
    assert combo["direction"] == "backward"
    assert combo["writer_tasks"] == ["writer_shipment_daily"]
    assert combo["reader_tasks"] == ["reader_ops_board"]


def test_advance_then_retry_same_stage_does_not_advance_twice():
    run_id = sample("A")["run_id"]
    assert client.post(f"/api/runs/{run_id}/rollout", json={"stage": 1}).status_code == 200
    first = client.post(f"/api/runs/{run_id}/rollout", json={"stage": 2})
    assert first.status_code == 200
    body = first.json()
    assert body["result"] == "advanced"
    assert body["execution"]["completed_stages"] == [1, 2]
    assert body["execution"]["current_stage"] == 3

    retry = client.post(f"/api/runs/{run_id}/rollout", json={"stage": 2})
    assert retry.status_code == 200
    assert retry.json()["result"] == "already_completed"
    execution = retry.json()["execution"]
    assert execution["completed_stages"] == [1, 2]
    assert execution["current_stage"] == 3

    saved = client.get(f"/api/runs/{run_id}").json()
    assert saved["rollout"]["execution"]["completed_stages"] == [1, 2]
    assert saved["summary"]["rollout_completed"] == 2
    assert saved["summary"]["rollout_current_stage"] == 3


def test_failed_stage_keeps_state_and_can_retry():
    run_id = sample("B")["run_id"]
    ok1 = client.post(f"/api/runs/{run_id}/rollout", json={})
    assert ok1.status_code == 200
    ok2 = client.post(f"/api/runs/{run_id}/rollout", json={})
    assert ok2.status_code == 200
    assert ok2.json()["execution"]["completed_stages"] == [1, 2]

    before = client.get(f"/api/runs/{run_id}").json()["rollout"]["execution"]
    fail = client.post(f"/api/runs/{run_id}/rollout", json={"stage": 3})
    assert fail.status_code == 409
    body = fail.json()
    assert body["result"] == "failed"
    assert body["execution"]["current_stage"] == before["current_stage"]
    assert body["execution"]["completed_stages"] == before["completed_stages"]
    assert body["execution"]["attempts"]["3"] == 1

    fail_again = client.post(f"/api/runs/{run_id}/rollout", json={"stage": 3})
    assert fail_again.status_code == 409
    assert fail_again.json()["execution"]["attempts"]["3"] == 2
    assert fail_again.json()["execution"]["completed_stages"] == before["completed_stages"]
    assert fail_again.json()["execution"]["current_stage"] == before["current_stage"]


def test_out_of_order_and_bad_stage_rejected():
    run_id = sample("A")["run_id"]
    res = client.post(f"/api/runs/{run_id}/rollout", json={"stage": 3})
    assert res.status_code == 409
    assert res.json()["result"] == "out_of_order"
    res = client.post(f"/api/runs/{run_id}/rollout", json={"stage": "3"})
    assert res.status_code == 400
    res = client.post(f"/api/runs/{run_id}/rollout", json={"stage": 99})
    assert res.status_code == 409
    assert res.json()["result"] == "invalid_stage"


def test_rollout_without_tasks_returns_409():
    old = {"table": "t", "fields": [{"id": 1, "name": "a", "type": "int32"}]}
    new = {"table": "t", "fields": [{"id": 1, "name": "a", "type": "int64"}]}
    consumers = {"table": "t", "files": [{"path": "p", "columns": ["a"]}]}
    run_id = post_compare({"old": old, "new": new, "consumers": consumers}).json()["run_id"]
    res = client.post(f"/api/runs/{run_id}/rollout", json={"stage": 1})
    assert res.status_code == 409
    assert "没有任务清单" in res.json()["detail"]


def test_bad_tasks_return_400():
    old = {"table": "t", "fields": [{"id": 1, "name": "a", "type": "int32"}]}
    new = {"table": "t", "fields": [{"id": 1, "name": "a", "type": "int64"}]}
    consumers = {"table": "t", "files": [{"path": "p", "columns": ["a"]}]}
    cases = [
        ({"tasks": [{"name": "x", "role": "worker", "version": "old"}]}, "role 必须是 writer 或 reader"),
        ({"tasks": [{"name": "x", "role": "writer", "version": "v2"}]}, "version 必须是 old 或 new"),
        ({"tasks": [{"name": "x", "role": "writer", "version": "new",
                     "switch_at": "2026-01-01T00:00:00Z"}]}, "switch_at 只用于描述 old→new"),
        ({"tasks": [{"name": "x", "role": "writer", "version": "old", "switch_at": "明天"}]}, "不是合法 ISO 8601"),
        ({"tasks": [{"name": "x", "role": "writer", "version": "old"},
                    {"name": "x", "role": "reader", "version": "old"}]}, "任务名重复"),
        ({"tasks": [{"name": "x", "role": "writer", "version": "old",
                     "start_at": "2026-01-02T00:00:00Z", "stop_at": "2026-01-01T00:00:00Z"}]},
         "不能晚于 stop_at"),
        ({"tasks": {}}, "tasks 数组"),
        ({"tasks": "[bad"}, "不是合法 JSON"),
    ]
    for extra, needle in cases:
        res = post_compare({"old": old, "new": new, "consumers": consumers, **extra})
        assert res.status_code == 400, extra
        assert needle in res.json()["detail"]


def test_result_page_renders_rollout_state():
    run_id = sample("B")["run_id"]
    client.post(f"/api/runs/{run_id}/rollout", json={})
    client.post(f"/api/runs/{run_id}/rollout", json={})
    client.post(f"/api/runs/{run_id}/rollout", json={"stage": 3})
    page = client.get(f"/result/{run_id}")
    assert page.status_code == 200
    assert "滚动切换窗口" in page.text
    assert "阶段 3/4" in page.text
    assert "已完成步骤" in page.text
    assert "新写旧读" in page.text
    assert "阶段 3 失败" in page.text
    assert "reader_dashboard" in page.text


def test_review_and_download_show_same_stage_state():
    run_id = sample("B")["run_id"]
    client.post(f"/api/runs/{run_id}/rollout", json={})
    client.post(f"/api/runs/{run_id}/rollout", json={})
    review = client.get("/review")
    assert review.status_code == 200
    assert "阶段 3/4" in review.text
    assert "阻断于阶段 3" in review.text
    sql = client.get(f"/api/runs/{run_id}/migration.sql").text
    assert "滚动切换计划（与页面同一状态）" in sql
    assert "已完成步骤 1、2" in sql
    assert "阶段 3/4" in sql
    assert "阻断组合" in sql


def test_workbench_shows_rollout_preview_and_tasks_input():
    page = client.get("/workbench?sample=C")
    assert page.status_code == 200
    assert "滚动切换窗口" in page.text
    assert "任务清单" in page.text
    tasks = client.get("/api/samples/C").json()
    assert "writer_shipment_daily" in tasks["tasks"]
