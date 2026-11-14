"""FastAPI 路由（薄）：页面渲染 + 少量 JSON API，核心规则全部在纯函数模块。"""
from __future__ import annotations

import json
import threading

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import report, schemaio, settings, store
from .rollout import advance
from .schemaio import SchemaError
from .types import DECIMAL_RULES, matrix_table

app = FastAPI(title=settings.APP_NAME)
app.mount("/static", StaticFiles(directory=str(settings.BASE_DIR / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(settings.BASE_DIR / "app" / "templates"))
_ROLLOUT_LOCK = threading.Lock()


def _ctx(request: Request, active: str, **extra) -> dict:
    ctx = {"app_name": settings.APP_NAME, "active": active, "matrix_version": settings.MATRIX_VERSION}
    ctx.update(extra)
    return ctx


def _index_stats(sample_count: int, history: list[dict]) -> dict:
    """总览页只读统计：全部从既有运行记录派生，不改动任何业务判定。"""
    steps = [int(r.get("summary", {}).get("sql_steps") or 0) for r in history]
    return {
        "sample_count": sample_count,
        "run_count": len(history),
        "full_count": sum(1 for r in history if r.get("summary", {}).get("level") == "full"),
        "backfill_count": sum(1 for r in history if int(r.get("summary", {}).get("backfill_count") or 0) > 0),
        "breaking_count": sum(1 for r in history if not r.get("summary", {}).get("can_execute", True)),
        "avg_steps": round(sum(steps) / len(steps), 1) if steps else 0.0,
    }


def _resolve_inputs(payload: dict) -> tuple[dict, dict, dict, list[dict], str]:
    if not isinstance(payload, dict):
        raise SchemaError("请求体必须是 JSON 对象")
    sample_key = str(payload.get("sample") or "").strip().upper()
    if sample_key in settings.SAMPLE_KEYS:
        sample = schemaio.load_sample(sample_key)
        return (sample["old"], sample["new"], sample["consumers"],
                sample["tasks"], f"样例 {sample_key}")
    old = schemaio.parse_schema(payload.get("old"), "旧 schema")
    new = schemaio.parse_schema(payload.get("new"), "新 schema")
    consumers = schemaio.parse_consumers(payload.get("consumers"), "数据文件清单")
    tasks = schemaio.parse_tasks(payload.get("tasks"), "任务清单")
    return old, new, consumers, tasks, "自定义输入"


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": "schemaevo", "matrix_version": settings.MATRIX_VERSION}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    samples = schemaio.load_sample_index()
    history = store.list_runs(limit=50)
    return templates.TemplateResponse(
        request, "index.html",
        _ctx(
            request, "/",
            page="index",
            samples=samples,
            recent=history[:3],
            stats=_index_stats(len(samples), history),
        ),
    )


@app.get("/workbench", response_class=HTMLResponse)
async def workbench(request: Request):
    key = (request.query_params.get("sample") or "A").strip().upper()
    if key not in settings.SAMPLE_KEYS:
        key = "A"
    sample = schemaio.load_sample(key)
    # 右侧面板「预演」：复用既有纯函数算一遍所选样例，不落库、不改接口。
    preview = report.build_run(sample["old"], sample["new"], sample["consumers"],
                               f"样例 {key}", sample["tasks"])
    return templates.TemplateResponse(
        request, "workbench.html",
        _ctx(
            request, "/workbench",
            page="workbench",
            samples=schemaio.load_sample_index(),
            selected=key,
            sample=sample,
            preview=preview,
            old_text=sample["raw_old"],
            new_text=sample["raw_new"],
            consumers_text=sample["raw_consumers"],
            tasks_text=sample["raw_tasks"],
        ),
    )


@app.post("/api/compare")
async def api_compare(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "请求体必须是 JSON，且 Content-Type 为 application/json"}, status_code=400)
    try:
        old, new, consumers, tasks, source = _resolve_inputs(payload)
        if old["table"] != new["table"]:
            raise SchemaError(f"新旧 schema 的表名不一致：{old['table']} vs {new['table']}")
        if consumers.get("table") and consumers["table"] != new["table"]:
            raise SchemaError(f"数据文件清单的表名 {consumers['table']} 与新 schema {new['table']} 不一致")
        run = report.build_run(old, new, consumers, source, tasks)
        run = store.save_run(run)
    except SchemaError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {
        "run_id": run["run_id"],
        "summary": run["summary"],
        "rollout": run["rollout"],
        "redirect": f"/result/{run['run_id']}",
    }


@app.get("/api/samples/{key}")
async def api_sample(key: str):
    try:
        sample = schemaio.load_sample(key)
    except SchemaError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    return {
        "key": sample["key"],
        "label": sample["label"],
        "description": sample["description"],
        "expected": sample["expected"],
        "old": sample["raw_old"],
        "new": sample["raw_new"],
        "consumers": sample["raw_consumers"],
        "tasks": sample["raw_tasks"],
    }


@app.get("/api/matrix")
async def api_matrix() -> dict:
    return {"version": settings.MATRIX_VERSION, "promotions": matrix_table(), "decimal_rules": DECIMAL_RULES}


@app.get("/api/runs")
async def api_runs() -> dict:
    rows = store.list_runs(limit=50)
    return {"count": len(rows), "runs": rows}


@app.get("/api/runs/{run_id}")
async def api_run(run_id: str):
    try:
        run = store.load_run(run_id)
    except KeyError:
        return JSONResponse({"detail": f"运行记录 {run_id} 不存在"}, status_code=404)
    return report.readiness(run)


@app.post("/api/runs/{run_id}/rollout")
async def api_rollout_advance(request: Request, run_id: str):
    """推进或重试一个切换阶段；失败保留原状态，重复提交已完成阶段不会重复推进。"""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse({"detail": "请求体必须是 JSON 对象"}, status_code=400)
    stage_id = payload.get("stage")
    if stage_id is not None and (isinstance(stage_id, bool) or not isinstance(stage_id, int)):
        return JSONResponse({"detail": "stage 必须是整数"}, status_code=400)
    with _ROLLOUT_LOCK:
        try:
            run = report.readiness(store.load_run(run_id))
        except KeyError:
            return JSONResponse({"detail": f"运行记录 {run_id} 不存在"}, status_code=404)
            return JSONResponse({"detail": f"运行记录 {run_id} 不存在"}, status_code=404)
        rollout = run.get("rollout") or {}
        if not rollout.get("available"):
            return JSONResponse({"detail": "该运行没有任务清单，无法执行切换阶段"}, status_code=409)
        execution, result = advance(rollout, rollout.get("execution") or {}, stage_id)
        run["rollout"] = rollout
        run["rollout"]["execution"] = execution
        run["summary"].update(report.execution_summary(run["rollout"]))
        store.save_run(run)
    result = dict(result)
    result["execution"] = execution
    result["summary"] = run["summary"]
    return JSONResponse(result, status_code=200 if result["ok"] else 409)


@app.get("/api/runs/{run_id}/migration.sql", response_class=PlainTextResponse)
async def api_sql(run_id: str):
    try:
        run = store.load_run(run_id)
    except KeyError:
        return PlainTextResponse(f"-- 运行记录 {run_id} 不存在\n", status_code=404)
    body = run["migration"]["sql_text"] + "\n" + report.rollout_text(report.readiness(run))
    return PlainTextResponse(
        body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{report.sql_download_name(run)}"'},
    )


@app.get("/result/{run_id}", response_class=HTMLResponse)
async def result_page(request: Request, run_id: str):
    try:
        run = report.readiness(store.load_run(run_id))
    except KeyError:
        return templates.TemplateResponse(
            request, "not_found.html",
            _ctx(
                request, "",
                page="not_found",
                run_id=run_id,
                max_runs=settings.MAX_RUNS_KEPT,
                error=f"运行记录 {run_id} 不存在（历史记录只保留最近 {settings.MAX_RUNS_KEPT} 次）",
            ),
            status_code=404,
        )
    return templates.TemplateResponse(
        request, "result.html",
        _ctx(request, "", page="result", run=run, overview=report.overview(run)),
    )


@app.get("/review", response_class=HTMLResponse)
async def review_page(request: Request):
    runs = store.list_runs(limit=50)
    latest = report.readiness(store.load_run(runs[0]["run_id"])) if runs else None
    return templates.TemplateResponse(
        request, "review.html",
        _ctx(
            request, "/review",
            page="review",
            runs=runs,
            run_count=len(runs),
            latest=latest,
            promotions=matrix_table(),
            decimal_rules=DECIMAL_RULES,
            sample_index=schemaio.load_sample_index(),
            max_runs=settings.MAX_RUNS_KEPT,
        ),
    )
