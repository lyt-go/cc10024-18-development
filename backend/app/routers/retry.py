"""失败采集重采输入包接口：导出、下载、接收、领取、补采和回执查询。

路由固定前缀都在 /api/retry 下，避免和通用资源路由 /api/{module} 互相抢占。
上传用多段表单（multipart/form-data），前端以 file 字段提交离线 zip。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app.services import retry

router = APIRouter(prefix="/api/retry", tags=["失败采集重采输入包"])

MAX_PACKAGE_BYTES = 20 * 1024 * 1024


def _call(func, *args, **kwargs) -> Any:
    try:
        return func(*args, **kwargs)
    except retry.RetryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/export-linkage")
def export_linkage() -> dict[str, Any]:
    """导出通道逐批交付视图：每个重采输入包关联原采集批次、包范围与取得时间。"""
    from app.services import SERVICES

    channel_service = SERVICES["export_channel"]
    return {"items": channel_service.list_retry_packages()}


@router.get("/runs")
def list_runs() -> dict[str, Any]:
    """可生成重采输入包的运行：带失败记录运行与失败页面的范围。"""
    return {"items": retry.list_runs_with_failures()}


@router.get("/runs/{run_id}/scope")
def run_scope(run_id: int) -> dict[str, Any]:
    """某运行的可重采范围；存量运行没有可选字段时也给出条件重采页面。"""
    scope = retry.run_failure_scope(run_id)
    if scope["失败总数"] == 0:
        raise HTTPException(status_code=404, detail=f"运行 {run_id} 没有失败采集记录")
    return scope


@router.get("/runs/{run_id}/export")
def export_run_failures(run_id: int, channel_id: int = Query(...),
                        batch_id: int | None = Query(default=None),
                        actor: str = Query(default=""),
                        shift: str = Query(default="")) -> FileResponse:
    """一步导出：生成重采输入包并直接下载（仍走已启用通道并留下交付关联）。"""
    package = _call(retry.create_package, run_id=run_id, channel_id=channel_id,
                    batch_id=batch_id, actor=actor, shift=shift)
    return FileResponse(
        path=package["文件路径"],
        media_type="application/zip",
        filename=package.get("文件名") or f"{package['包编号']}.zip",
    )


@router.get("/results")
def list_results(run_id: int | None = Query(default=None),
                 page_key: str | None = Query(default=None),
                 page_address: str | None = Query(default=None)) -> dict[str, Any]:
    """查询补采结果：可按运行、页面键或页面地址核对同一页是否已有结果。"""
    items = retry.workbench(run_id)["已补采结果"]
    if page_key:
        items = [item for item in items if item.get("页面键") == page_key]
    if page_address:
        matched = {item.get("页面键") for item in items
                   if page_address == item.get("页面地址")}
        items = [item for item in items
                 if item.get("页面键") == page_address or item.get("页面键") in matched]
    return {"items": items}


@router.get("/failures")
def list_failures(run_id: int | None = Query(default=None)) -> dict[str, Any]:
    """原始失败采集记录（供离线核查前的在线预览）。"""
    rows = retry.failures_for_run(run_id) if run_id is not None else list(
        retry.store.rows(retry.FAILURE_TABLE))
    return {"items": [dict(row) for row in rows]}


@router.post("/packages")
def create_package(payload: dict[str, Any]) -> dict[str, Any]:
    """通过已启用的导出通道生成重采输入包（文件落到 exports/，可直接下载）。"""
    run_id = payload.get("run_id")
    channel_id = payload.get("channel_id")
    if not isinstance(run_id, int) or not isinstance(channel_id, int):
        raise HTTPException(status_code=400, detail="需要 run_id 与 channel_id")
    package = _call(
        retry.create_package,
        run_id=run_id,
        channel_id=channel_id,
        batch_id=payload.get("batch_id"),
        actor=str(payload.get("actor") or ""),
        shift=str(payload.get("shift") or ""),
    )
    return {"ok": True, "message": f"重采输入包 {package['包编号']} 已出库", "package": package}


@router.get("/packages")
def list_packages(run_id: int | None = Query(default=None),
                  channel_id: int | None = Query(default=None)) -> dict[str, Any]:
    return {"items": retry.list_packages(run_id=run_id, channel_id=channel_id)}


@router.get("/packages/{package_id}")
def get_package(package_id: int) -> dict[str, Any]:
    package = retry.store.find(retry.PACKAGE_TABLE, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"重采输入包 {package_id} 不存在")
    return package


@router.get("/packages/{package_id}/download")
def download_package(package_id: int) -> FileResponse:
    """下载离线压缩包，交给另一值班人离线核查。"""
    package = _call(retry.package_download_path, package_id)
    return FileResponse(
        path=package["文件路径"],
        media_type="application/zip",
        filename=package.get("文件名") or f"{package['包编号']}.zip",
    )


@router.post("/receive")
async def receive(
    file: UploadFile = File(...),
    actor: str = Form(default=""),
    shift: str = Form(default=""),
) -> JSONResponse:
    """接收离线压缩包：核对可执行范围、登记差异，重复接收不重复发放资格。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(raw) > MAX_PACKAGE_BYTES:
        raise HTTPException(status_code=413, detail="重采输入包超过 20MB 上限")
    result = _call(retry.receive_package, raw=raw, filename=file.filename or "",
                   actor=actor, shift=shift)
    return JSONResponse(result)


@router.get("/workbench")
def workbench(run_id: int | None = Query(default=None)) -> dict[str, Any]:
    """恢复入口视图：剩余工作、可领取、已补采结果、回执与包。"""
    return retry.workbench(run_id)


@router.get("/items")
def list_items(run_id: int | None = Query(default=None),
               state: str | None = Query(default=None),
               eligible: bool | None = Query(default=None)) -> dict[str, Any]:
    return {"items": retry.list_items(run_id=run_id, state=state, eligible=eligible)}


@router.post("/items/{item_id}/claim")
def claim(item_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """领取一项剩余重采工作。"""
    item = _call(retry.claim_item, item_id,
                 actor=str(payload.get("actor") or ""),
                 shift=str(payload.get("shift") or ""))
    return {"ok": True, "message": f"已领取页面 {item['页面键']}", "item": item,
            "剩余工作": retry.workbench(_as_int(item.get("运行id")))["剩余工作"]}


@router.post("/items/{item_id}/complete")
def complete(item_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """登记某个工作项的补采结果。"""
    result = _call(
        retry.complete_item, item_id,
        result_summary=str(payload.get("result_summary") or ""),
        result_source=str(payload.get("result_source") or "重采完成"),
        result_snapshot_id=payload.get("result_snapshot_id"),
        actor=str(payload.get("actor") or ""),
        shift=str(payload.get("shift") or ""),
    )
    return result


@router.post("/completions")
def complete_by_page(payload: dict[str, Any]) -> dict[str, Any]:
    """在别处补采后，按运行 + 页面键/页面地址登记补采结果。"""
    result = _call(
        retry.complete_page,
        run_id=payload.get("run_id"),
        page_key_value=str(payload.get("page_key") or ""),
        page_address_value=str(payload.get("page_address") or ""),
        result_summary=str(payload.get("result_summary") or ""),
        result_source=str(payload.get("result_source") or "外部补采"),
        result_snapshot_id=payload.get("result_snapshot_id"),
        actor=str(payload.get("actor") or ""),
        shift=str(payload.get("shift") or ""),
    )
    return result


@router.get("/receipts")
def list_receipts(run_id: int | None = Query(default=None)) -> dict[str, Any]:
    rows = [dict(row) for row in retry.store.rows(retry.RECEIPT_TABLE)]
    if run_id is not None:
        rows = [row for row in rows if _as_int(row.get("运行id")) == run_id]
    return {"items": list(reversed(rows))}


@router.get("/receipts/{receipt_id}")
def get_receipt(receipt_id: int) -> dict[str, Any]:
    receipt = retry.store.find(retry.RECEIPT_TABLE, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"回执 {receipt_id} 不存在")
    return receipt


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
