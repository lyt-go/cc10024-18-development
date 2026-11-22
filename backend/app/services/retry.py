"""失败采集重采输入包领域服务。

职责：
1. 从 run_history 的失败采集明细生成可离线核查的原始失败记录包（zip）；
2. 通过 export_channel 通道逐批交付，并保留“原采集批次 / 包范围 / 取得时间”的关联；
3. 接收（重新导入）离线包时核对可执行范围，给出与原包的差异；
4. 只在页面尚未补采时发放一次执行资格，重复接收包或回执不重复发放；
5. 记录页面补采结果，支持在别处补采后再核对同一页的结果。

内部表都以 `_` 开头，不出现在运营概览的业务模块列表里。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from typing import Any

from app import persistence as persist
from app.services.base import register_hook
from app.store import store

FAILURE_TABLE = "_capture_failure"
PACKAGE_TABLE = "_retry_package"
ITEM_TABLE = "_retry_item"
RECEIPT_TABLE = "_retry_receipt"
COMPLETION_TABLE = "_retry_completion"

PACKAGE_SCHEMA = "retry-input-package/1"
# 这些错误类型属于确定性失败，即使没有“可重采”字段也判定为不可重采。
NON_RETRYABLE_ERRORS = {"页面已删除", "权限拒绝", "永久失效", "404", "410"}


class RetryError(ValueError):
    """重采输入包业务校验失败。"""


def _stable(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def _sha256(data: Any) -> str:
    return hashlib.sha256(_stable(data).encode("utf-8")).hexdigest()


def _next_id(table: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def page_key(failure: dict[str, Any]) -> str:
    """页面定位键：页面键优先，其次快照编号 / 页面地址，最后退回失败记录 id。"""
    return str(
        failure.get("页面键")
        or failure.get("快照编号")
        or failure.get("页面地址")
        or f"失败{failure.get('id')}"
    )


def page_address(failure: dict[str, Any]) -> str:
    """页面地址是可选字段，存量记录可能没有。"""
    return str(failure.get("页面地址") or "")


def retryable(failure: dict[str, Any]) -> tuple[bool, str]:
    """判断某页是否“有条件重采”。字段缺失时仍按错误类型/重试边界给出结论。"""
    flag = failure.get("可重采")
    error_type = str(failure.get("错误类型") or "")
    boundary = str(failure.get("重试边界") or "")
    if isinstance(flag, bool):
        if not flag:
            return False, str(failure.get("不可重采原因") or "记录标记为不可重采")
        return True, boundary or "按重试边界重采"
    if error_type in NON_RETRYABLE_ERRORS:
        return False, f"错误类型“{error_type}”不可重采"
    if "不重试" in boundary or "放弃" in boundary:
        return False, f"重试边界“{boundary}”不允许重采"
    return True, boundary or "按失败步骤重采（存量记录无重试边界，默认有条件重采）"


def failures_for_run(run_id: int) -> list[dict[str, Any]]:
    """读取某次运行的原始失败采集记录，兼容只有关联计划的存量记录。"""
    rows = [row for row in store.rows(FAILURE_TABLE)
            if _as_int(row.get("运行id")) == run_id]
    if rows:
        return sorted(rows, key=lambda row: int(row.get("id", 0)))
    run = store.find("run_history", run_id) or {}
    plan = run.get("关联计划")
    if not plan:
        return []
    # 存量运行可能没有写入运行 id，只保留了关联计划。
    return sorted(
        [row for row in store.rows(FAILURE_TABLE)
         if not row.get("运行id") and row.get("关联计划") == plan],
        key=lambda row: int(row.get("id", 0)),
    )


def run_failure_scope(run_id: int) -> dict[str, Any]:
    """输出某运行的可重采范围，缺失可选字段时也给出条件重采页面。"""
    run = store.find("run_history", run_id) or {}
    failures = failures_for_run(run_id)
    retry_pages: list[dict[str, Any]] = []
    blocked_pages: list[dict[str, Any]] = []
    missing_optional: set[str] = set()
    for failure in failures:
        for optional in ("页面地址", "采集批次id", "可重采"):
            if optional not in failure or failure.get(optional) in (None, ""):
                missing_optional.add(optional)
        allowed, reason = retryable(failure)
        payload = {
            "页面键": page_key(failure),
            "页面地址": page_address(failure),
            "失败步骤": failure.get("失败步骤", ""),
            "错误类型": failure.get("错误类型", ""),
            "重试边界": failure.get("重试边界", ""),
            "失败记录id": failure.get("id"),
        }
        if allowed:
            payload["重采条件"] = reason
            retry_pages.append(payload)
        else:
            payload["不可重采原因"] = reason
            blocked_pages.append(payload)
    return {
        "run_id": run_id,
        "记录编号": run.get("记录编号", ""),
        "关联计划": run.get("关联计划", ""),
        "失败总数": len(failures),
        "可重采页面": retry_pages,
        "不可重采页面": blocked_pages,
        "缺失可选字段": sorted(missing_optional),
        "存量运行": bool(missing_optional),
    }


def list_runs_with_failures() -> list[dict[str, Any]]:
    """列出所有可生成重采输入包的运行，供导出入口选择。"""
    run_ids: set[int] = set()
    for row in store.rows(FAILURE_TABLE):
        rid = _as_int(row.get("运行id"))
        if rid is not None:
            run_ids.add(rid)
            continue
        if row.get("关联计划"):
            for run in store.rows("run_history"):
                if run.get("关联计划") == row.get("关联计划"):
                    run_ids.add(int(run.get("id", 0)))
    scopes = [run_failure_scope(rid) for rid in sorted(run_ids)]
    return [scope for scope in scopes if scope["失败总数"]]


def _fingerprint(run_id: int, batch_id: int | None, failures: list[dict[str, Any]]) -> str:
    return _sha256({
        "run_id": run_id,
        "batch_id": batch_id,
        "failures": sorted(failures, key=lambda row: int(row.get("id", 0))),
    })


def _package_payload(run: dict[str, Any], channel: dict[str, Any], entries: list[dict[str, Any]],
                     blocked: list[dict[str, Any]], batch_id: int | None, acquired: str,
                     fingerprint: str, actor: str, shift: str, package_no: str) -> dict[str, Any]:
    batch = store.find("batch_queue", batch_id) if batch_id else None
    return {
        "schema": PACKAGE_SCHEMA,
        "包编号": package_no,
        "来源运行id": int(run.get("id", 0)),
        "来源运行记录编号": run.get("记录编号", ""),
        "关联计划": run.get("关联计划", ""),
        "原采集批次id": batch_id,
        "原采集批次编号": (batch or {}).get("批次编号", ""),
        "通道id": int(channel.get("id", 0)),
        "通道编号": channel.get("通道编号", ""),
        "取得时间": acquired,
        "内容指纹": fingerprint,
        "包范围": {
            "页面数": len(entries),
            "页面键": [page_key(item) for item in entries],
            "页面": [
                {
                    "页面键": page_key(item),
                    "页面地址": page_address(item),
                    "失败步骤": item.get("失败步骤", ""),
                    "错误类型": item.get("错误类型", ""),
                    "重采条件": retryable(item)[1],
                }
                for item in entries
            ],
        },
        "不可执行页面": [
            {"页面键": page_key(item), "原因": retryable(item)[1]} for item in blocked
        ],
        "可选字段说明": "页面地址、采集批次为可选字段；缺失时仍以页面键确定可重采页面",
        "导出人": actor,
        "班次": shift,
    }


def _write_package_file(package: dict[str, Any], manifest: dict[str, Any],
                        failures: list[dict[str, Any]]) -> str:
    filename = f"{package['包编号']}_run{package['来源运行id']}.zip"
    path = persist.data_path("exports", filename)
    readme = (
        "重采输入包（离线核查用）\n"
        f"包编号：{manifest['包编号']}\n"
        f"来源运行：{manifest['来源运行记录编号']}（id={manifest['来源运行id']}）\n"
        f"原采集批次：{manifest['原采集批次编号'] or '未记录'}（id={manifest['原采集批次id']}）\n"
        f"取得时间：{manifest['取得时间']}\n"
        f"包范围：{manifest['包范围']['页面数']} 个可重采页面\n"
        "manifest.json    包清单与范围\n"
        "failures.json    原始失败采集记录（离线核查）\n"
        "该文件可通过“错误恢复 → 重采输入包接收”重新导入。\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("failures.json", json.dumps(
            {"包编号": manifest["包编号"], "来源运行id": manifest["来源运行id"],
             "记录": failures}, ensure_ascii=False, indent=2))
        archive.writestr("README.txt", readme)
    with open(path, "wb") as handle:
        handle.write(buffer.getvalue())
    package["文件名"] = filename
    package["文件路径"] = path
    package["文件大小"] = len(buffer.getvalue())
    return path


def create_package(*, run_id: int, channel_id: int, batch_id: int | None = None,
                   actor: str = "", shift: str = "") -> dict[str, Any]:
    """通过已启用的导出通道生成一份重采输入包，并登记与原采集批次的关联。"""
    run = store.find("run_history", run_id)
    if run is None:
        raise RetryError(f"运行记录 {run_id} 不存在或已归档")
    channel = store.find("export_channel", channel_id)
    if channel is None:
        raise RetryError(f"导出通道 {channel_id} 不存在")
    if channel.get("status") != "已启用":
        raise RetryError(f"导出通道 {channel.get('通道编号') or channel_id} 未启用，不能交付重采输入包")

    failures = failures_for_run(run_id)
    if not failures:
        raise RetryError(f"运行 {run.get('记录编号') or run_id} 没有失败采集记录")

    entries: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for failure in failures:
        allowed, _ = retryable(failure)
        (entries if allowed else blocked).append(failure)
    if not entries:
        raise RetryError("该运行的失败页面均无重采条件，不能生成重采输入包")

    if batch_id is None:
        batch_id = next(
            (_as_int(row.get("采集批次id")) for row in failures
             if _as_int(row.get("采集批次id")) is not None), None)
    package_no = f"RPKG-{_next_id(PACKAGE_TABLE):04d}"
    acquired = persist.now_iso()
    fingerprint = _fingerprint(run_id, batch_id, failures)
    manifest = _package_payload(run, channel, entries, blocked, batch_id, acquired,
                                fingerprint, actor, shift, package_no)
    package: dict[str, Any] = {
        "id": _next_id(PACKAGE_TABLE),
        "包编号": package_no,
        "来源运行id": int(run_id),
        "来源运行记录编号": run.get("记录编号", ""),
        "关联计划": run.get("关联计划", ""),
        "原采集批次id": batch_id,
        "原采集批次编号": manifest["原采集批次编号"],
        "通道id": int(channel_id),
        "通道编号": channel.get("通道编号", ""),
        "取得时间": acquired,
        "内容指纹": fingerprint,
        "页面数": len(entries),
        "包范围": manifest["包范围"]["页面键"],
        "不可执行页面": [item["页面键"] for item in manifest["不可执行页面"]],
        "导出人": actor,
        "班次": shift,
        "包清单": manifest,
        "status": "已出库",
        "pending": False,
        "abnormal": False,
        "version": fingerprint[:12],
        "updated_at": acquired,
    }
    _write_package_file(package, manifest, failures)
    store.rows(PACKAGE_TABLE).append(package)
    persist.append_jsonl("runs", "retry_package.jsonl", {
        "module": "retry_package", "entity": "重采输入包", "entry_id": package["id"],
        "package_no": package_no, "run_id": run_id, "batch_id": batch_id,
        "channel_id": channel_id, "scope": package["包范围"],
        "acquired_at": acquired, "fingerprint": fingerprint,
        "action": "export_retry_package", "timestamp": acquired, "abnormal": False,
    })
    return package


def _find_completion(run_id: int, key: str) -> dict[str, Any] | None:
    for row in reversed(store.rows(COMPLETION_TABLE)):
        if _as_int(row.get("运行id")) == run_id and row.get("页面键") == key:
            return row
    return None


def _find_item(run_id: int, key: str) -> dict[str, Any] | None:
    for row in store.rows(ITEM_TABLE):
        if _as_int(row.get("运行id")) == run_id and row.get("页面键") == key:
            return row
    return None


def _item_view(item: dict[str, Any]) -> dict[str, Any]:
    return dict(item)


def _remaining(run_id: int | None = None) -> list[dict[str, Any]]:
    rows = store.rows(ITEM_TABLE)
    if run_id is not None:
        rows = [row for row in rows if _as_int(row.get("运行id")) == run_id]
    return [
        _item_view(row) for row in rows
        if row.get("执行资格") and row.get("状态") in ("待补采", "已领取")
    ]


def _completions(run_id: int | None = None) -> list[dict[str, Any]]:
    rows = store.rows(COMPLETION_TABLE)
    if run_id is not None:
        rows = [row for row in rows if _as_int(row.get("运行id")) == run_id]
    return [dict(row) for row in rows]


def _register_imported_package(manifest: dict[str, Any], raw: bytes) -> dict[str, Any]:
    """接收来源实例之外生成的包：按清单登记，并把原始文件留档。"""
    existing = next((row for row in store.rows(PACKAGE_TABLE)
                     if row.get("包编号") == manifest.get("包编号")), None)
    if existing:
        return existing
    package_no = str(manifest.get("包编号") or f"RPKG-IMPORT-{_next_id(PACKAGE_TABLE):04d}")
    filename = f"{package_no}_imported.zip"
    path = persist.data_path("exports", filename)
    with open(path, "wb") as handle:
        handle.write(raw)
    acquired = str(manifest.get("取得时间") or persist.now_iso())
    package = {
        "id": _next_id(PACKAGE_TABLE),
        "包编号": package_no,
        "来源运行id": _as_int(manifest.get("来源运行id")),
        "来源运行记录编号": manifest.get("来源运行记录编号", ""),
        "关联计划": manifest.get("关联计划", ""),
        "原采集批次id": _as_int(manifest.get("原采集批次id")),
        "原采集批次编号": manifest.get("原采集批次编号", ""),
        "通道id": _as_int(manifest.get("通道id")),
        "通道编号": manifest.get("通道编号", ""),
        "取得时间": acquired,
        "内容指纹": manifest.get("内容指纹", ""),
        "页面数": int((manifest.get("包范围") or {}).get("页面数") or 0),
        "包范围": list((manifest.get("包范围") or {}).get("页面键") or []),
        "不可执行页面": [
            item.get("页面键", "") for item in (manifest.get("不可执行页面") or [])
        ],
        "导出人": manifest.get("导出人", ""),
        "班次": manifest.get("班次", ""),
        "包清单": manifest,
        "status": "导入",
        "pending": False,
        "abnormal": False,
        "version": str(manifest.get("内容指纹", ""))[:12],
        "updated_at": persist.now_iso(),
    }
    package["文件名"] = filename
    package["文件路径"] = path
    package["文件大小"] = len(raw)
    store.rows(PACKAGE_TABLE).append(package)
    persist.append_jsonl("runs", "retry_package.jsonl", {
        "module": "retry_package", "entity": "重采输入包", "entry_id": package["id"],
        "package_no": package_no, "run_id": package["来源运行id"],
        "batch_id": package["原采集批次id"], "action": "import_retry_package",
        "timestamp": persist.now_iso(), "abnormal": False,
    })
    return package


def _parse_package(raw: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = set(archive.namelist())
            if "manifest.json" not in names or "failures.json" not in names:
                raise RetryError("压缩包缺少 manifest.json 或 failures.json，不是有效的重采输入包")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            failures = json.loads(archive.read("failures.json").decode("utf-8"))
    except zipfile.BadZipFile as exc:
        raise RetryError("上传文件不是有效的离线压缩包（zip）") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetryError("重采输入包清单无法解析") from exc
    records = failures.get("记录") if isinstance(failures, dict) else failures
    if not isinstance(manifest, dict) or not isinstance(records, list):
        raise RetryError("重采输入包清单结构不正确")
    if not manifest.get("包编号") or _as_int(manifest.get("来源运行id")) is None:
        raise RetryError("重采输入包缺少包编号或来源运行")
    expected = manifest.get("内容指纹")
    actual = _fingerprint(
        int(manifest["来源运行id"]),
        _as_int(manifest.get("原采集批次id")),
        records,
    )
    if expected and expected != actual:
        raise RetryError("重采输入包内容与清单指纹不一致，可能被修改过")
    return manifest, records


def receive_package(*, raw: bytes, filename: str = "", actor: str = "",
                    shift: str = "") -> dict[str, Any]:
    """接收离线包：核对可执行范围，登记与原包的差异，幂等发放执行资格。"""
    manifest, records = _parse_package(raw)
    package = _register_imported_package(manifest, raw)
    run_id = int(manifest["来源运行id"])
    fingerprint = str(manifest.get("内容指纹") or "")
    scope = list((manifest.get("包范围") or {}).get("页面键") or [])

    prior = [row for row in store.rows(RECEIPT_TABLE)
             if _as_int(row.get("运行id")) == run_id
             and row.get("包编号") == package.get("包编号")]
    duplicate = bool(prior)

    diff: dict[str, list[dict[str, Any]]] = {
        "新增可执行": [],
        "已存在不重复发放": [],
        "已补采跳过": [],
        "已失效页面": [],
        "包外可重采": [],
    }
    new_eligibility = 0
    for key in scope:
        completion = _find_completion(run_id, key)
        item = _find_item(run_id, key)
        if completion is not None:
            if item is None:
                # 留下可追溯的已补采结果，但没有执行资格。
                item = _new_item(package, manifest, key, state="已补采", eligible=False)
                _apply_completion(item, completion)
            else:
                item["状态"] = "已补采"
                item["执行资格"] = False
                _apply_completion(item, completion)
            diff["已补采跳过"].append({
                "页面键": key,
                "补采结果": completion.get("结果摘要", ""),
                "结果来源": completion.get("结果来源", ""),
                "完成时间": completion.get("完成时间", ""),
            })
        elif item is not None:
            if item.get("执行资格") and item.get("状态") in ("待补采", "已领取"):
                diff["已存在不重复发放"].append({"页面键": key, "状态": item.get("状态")})
            else:
                diff["已失效页面"].append({"页面键": key, "原因": "工作项已失效"})
        else:
            _new_item(package, manifest, key, state="待补采", eligible=True)
            new_eligibility += 1
            diff["新增可执行"].append({"页面键": key})

    current_retryable = {
        page_key(row) for row in failures_for_run(run_id) if retryable(row)[0]
    }
    # 只有本地还留有该运行的失败明细时，才能断言“包内页面已不可重采”；
    # 外部包（本实例没有失败明细）只登记包外可重采页，不做失效判定。
    if current_retryable:
        for key in sorted(current_retryable - set(scope)):
            diff["包外可重采"].append({"页面键": key, "原因": "当前运行还有包外的可重采页面"})
        for key in sorted(set(scope) - current_retryable):
            if not any(entry["页面键"] == key for entry in diff["已补采跳过"]) \
                    and not any(entry["页面键"] == key for entry in diff["已失效页面"]):
                diff["已失效页面"].append({"页面键": key, "原因": "该页已不在当前可重采范围"})

    receipt = {
        "id": _next_id(RECEIPT_TABLE),
        "回执编号": f"RCPT-{_next_id(RECEIPT_TABLE):04d}",
        "包id": package.get("id"),
        "包编号": package.get("包编号", manifest.get("包编号")),
        "运行id": run_id,
        "内容指纹": fingerprint,
        "文件名": filename,
        "操作人": actor,
        "班次": shift,
        "接收时间": persist.now_iso(),
        "重复接收": duplicate,
        "新增执行资格": new_eligibility,
        "原包范围": scope,
        "当前可执行范围": sorted(current_retryable),
        "差异": diff,
        "status": "已接收",
        "pending": False,
        "abnormal": False,
        "updated_at": persist.now_iso(),
    }
    store.rows(RECEIPT_TABLE).append(receipt)
    persist.append_jsonl("runs", "retry_receipt.jsonl", {
        "module": "retry_receipt", "entity": "重采回执", "entry_id": receipt["id"],
        "receipt_no": receipt["回执编号"], "package_no": receipt["包编号"],
        "run_id": run_id, "duplicate": duplicate,
        "new_eligibility": new_eligibility, "diff": diff,
        "action": "receive_retry_package", "timestamp": receipt["接收时间"],
        "abnormal": False,
    })
    return {
        "ok": True,
        "message": (
            f"重复接收：未新增执行资格，剩余工作 {len(_remaining(run_id))} 项"
            if duplicate else
            f"已接收重采输入包，新增执行资格 {new_eligibility} 项"
        ),
        "重复接收": duplicate,
        "新增执行资格": new_eligibility,
        "回执编号": receipt["回执编号"],
        "回执": receipt,
        "包": package,
        "差异": diff,
        "剩余工作": _remaining(run_id),
        "已补采结果": _completions(run_id),
    }


def _new_item(package: dict[str, Any], manifest: dict[str, Any], key: str, *,
              state: str, eligible: bool) -> dict[str, Any]:
    page = next(
        (entry for entry in (manifest.get("包范围") or {}).get("页面", [])
         if entry.get("页面键") == key), {})
    run_id = int(manifest["来源运行id"])
    failure = next((row for row in failures_for_run(run_id) if page_key(row) == key), {})
    item = {
        "id": _next_id(ITEM_TABLE),
        "包id": package.get("id"),
        "包编号": package.get("包编号", manifest.get("包编号")),
        "运行id": run_id,
        "来源运行记录编号": manifest.get("来源运行记录编号", ""),
        "原采集批次id": _as_int(manifest.get("原采集批次id")),
        "原采集批次编号": manifest.get("原采集批次编号", ""),
        "页面键": key,
        "页面地址": page.get("页面地址") or page_address(failure),
        "失败步骤": page.get("失败步骤") or failure.get("失败步骤", ""),
        "错误类型": page.get("错误类型") or failure.get("错误类型", ""),
        "重试边界": failure.get("重试边界", ""),
        "状态": state,
        "执行资格": eligible,
        "领取人": "",
        "领取时间": "",
        "班次": "",
        "结果摘要": "",
        "结果来源": "",
        "结果快照id": None,
        "完成时间": "",
        "status": state,
        "pending": state != "已补采",
        "abnormal": False,
        "version": "",
        "updated_at": persist.now_iso(),
    }
    item["version"] = _sha256(item)[:12]
    store.rows(ITEM_TABLE).append(item)
    return item


def _apply_completion(item: dict[str, Any], completion: dict[str, Any]) -> None:
    item["结果摘要"] = completion.get("结果摘要", "")
    item["结果来源"] = completion.get("结果来源", "")
    item["结果快照id"] = completion.get("结果快照id")
    item["完成时间"] = completion.get("完成时间", "")
    item["状态"] = "已补采"
    item["执行资格"] = False
    item["status"] = "已补采"
    item["pending"] = False
    item["updated_at"] = persist.now_iso()


def _resolve_page_reference(run_id: int | None, key: str,
                            address: str) -> tuple[str, int | None, str]:
    """把页面地址解析成页面键：先看包外可重采页，再看失败明细，最后用地址本身作键。"""
    reference = (key or address).strip()
    if not reference:
        return key, run_id, address

    def match(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        for row in rows:
            candidate_key = str(row.get("页面键") or "")
            candidate_address = str(row.get("页面地址") or "")
            if key and key == candidate_key:
                return row
            if address and address == candidate_address:
                return row
            if not key and not address and reference in (candidate_key, candidate_address):
                return row
        return None

    if run_id is not None:
        item = match([row for row in store.rows(ITEM_TABLE)
                      if _as_int(row.get("运行id")) == run_id])
        if item is not None:
            return str(item.get("页面键")), run_id, address or str(item.get("页面地址") or "")
    for candidate_run in sorted({_as_int(row.get("运行id")) for row in store.rows(ITEM_TABLE)
                                 if _as_int(row.get("运行id")) is not None}):
        item = match([row for row in store.rows(ITEM_TABLE)
                      if _as_int(row.get("运行id")) == candidate_run])
        if item is not None:
            return str(item.get("页面键")), candidate_run, address or str(item.get("页面地址") or "")
    for failure in store.rows(FAILURE_TABLE):
        item = match([{"页面键": page_key(failure), "页面地址": page_address(failure)}])
        if item is not None:
            failure_run = _as_int(failure.get("运行id"))
            if failure_run is None and run_id is not None:
                failure_run = run_id
            return page_key(failure), failure_run, address or page_address(failure)
    if run_id is None:
        return key, run_id, address
    return (key or page_key({"页面地址": address, "id": "external"}), run_id,
            address or key)


def list_items(*, run_id: int | None = None, state: str | None = None,
               eligible: bool | None = None) -> list[dict[str, Any]]:
    rows = store.rows(ITEM_TABLE)
    result = list(rows)
    if run_id is not None:
        result = [row for row in result if _as_int(row.get("运行id")) == run_id]
    if state:
        result = [row for row in result if row.get("状态") == state]
    if eligible is not None:
        result = [row for row in result if bool(row.get("执行资格")) is eligible]
    return [dict(row) for row in sorted(result, key=lambda row: int(row.get("id", 0)))]


def claim_item(item_id: int, *, actor: str, shift: str = "") -> dict[str, Any]:
    item = store.find(ITEM_TABLE, item_id)
    if item is None:
        raise RetryError(f"重采工作项 {item_id} 不存在")
    if item.get("状态") == "已补采":
        raise RetryError("该页面已补采，不能再领取")
    if not item.get("执行资格"):
        raise RetryError("该页面没有执行资格")
    holder = item.get("领取人") or item.get("班次")
    if item.get("状态") == "已领取" and holder not in (actor, shift):
        raise RetryError(f"该页面已被 {holder or '其他值班人'} 领取")
    item["状态"] = "已领取"
    item["status"] = "已领取"
    item["领取人"] = item.get("领取人") or actor
    item["班次"] = item.get("班次") or shift
    item["领取时间"] = item.get("领取时间") or persist.now_iso()
    item["pending"] = True
    item["updated_at"] = persist.now_iso()
    item["version"] = _sha256(item)[:12]
    return dict(item)


def complete_item(item_id: int, *, result_summary: str, result_source: str = "重采完成",
                  result_snapshot_id: int | None = None, actor: str = "",
                  shift: str = "") -> dict[str, Any]:
    item = store.find(ITEM_TABLE, item_id)
    if item is None:
        raise RetryError(f"重采工作项 {item_id} 不存在")
    return complete_page(
        run_id=_as_int(item.get("运行id")),
        page_key_value=str(item.get("页面键")),
        result_summary=result_summary,
        result_source=result_source,
        result_snapshot_id=result_snapshot_id,
        actor=actor,
        shift=shift,
        package_no=str(item.get("包编号") or ""),
    )


def complete_page(*, run_id: int | None = None, page_key_value: str = "",
                  page_address_value: str = "", result_summary: str,
                  result_source: str = "外部补采", result_snapshot_id: int | None = None,
                  actor: str = "", shift: str = "", package_no: str = "") -> dict[str, Any]:
    """登记某页的补采结果；同一运行同一页只保留一份结果，重复补采幂等。"""
    if not str(result_summary or "").strip():
        raise RetryError("补采结果摘要不能为空")
    resolved_run = run_id
    resolved_key = page_key_value
    resolved_address = page_address_value
    if resolved_run is None or not resolved_key:
        candidates: list[dict[str, Any]] = []
        for item in store.rows(ITEM_TABLE):
            if resolved_run is not None and _as_int(item.get("运行id")) != resolved_run:
                continue
            if resolved_key and item.get("页面键") != resolved_key:
                continue
            if resolved_address and resolved_address not in (
                    str(item.get("页面地址") or ""), str(item.get("页面键") or "")):
                continue
            if resolved_key or resolved_address:
                candidates.append(item)
        if candidates:
            resolved_run = _as_int(candidates[0].get("运行id"))
            resolved_key = str(candidates[0].get("页面键"))
            resolved_address = resolved_address or str(candidates[0].get("页面地址") or "")
    if resolved_run is None or not resolved_key:
        # 之后才出现的页面（例如离线包尚未接收）按地址或页面键补齐运行与页面键。
        resolved_key, resolved_run, resolved_address = _resolve_page_reference(
            resolved_run, resolved_key, resolved_address)
    if resolved_run is None or not resolved_key:
        raise RetryError("无法确定要补采的页面，请提供运行与页面键或页面地址")

    existing = _find_completion(resolved_run, resolved_key)
    if existing is not None:
        stale = _find_item(resolved_run, resolved_key)
        if stale is not None and stale.get("状态") != "已补采":
            _apply_completion(stale, existing)
            stale["version"] = _sha256(stale)[:12]
        return {
            "ok": True,
            "message": "该页面已有补采结果，未重复登记",
            "重复完成": True,
            "结果": existing,
            "剩余工作": _remaining(resolved_run),
        }

    completion = {
        "id": _next_id(COMPLETION_TABLE),
        "结果编号": f"RETRY-RESULT-{_next_id(COMPLETION_TABLE):04d}",
        "运行id": resolved_run,
        "页面键": resolved_key,
        "页面地址": resolved_address or (
            resolved_key if resolved_key.startswith(("http://", "https://")) else ""),
        "结果摘要": str(result_summary).strip(),
        "结果来源": result_source,
        "结果快照id": result_snapshot_id,
        "操作人": actor,
        "班次": shift,
        "包编号": package_no,
        "完成时间": persist.now_iso(),
        "status": "已补采",
        "pending": False,
        "abnormal": False,
        "updated_at": persist.now_iso(),
    }
    store.rows(COMPLETION_TABLE).append(completion)
    for item in store.rows(ITEM_TABLE):
        if _as_int(item.get("运行id")) == resolved_run and item.get("页面键") == resolved_key:
            item["执行资格"] = False
            _apply_completion(item, completion)
            item["version"] = _sha256(item)[:12]
    persist.append_jsonl("runs", "retry_completion.jsonl", {
        "module": "retry_completion", "entity": "补采结果", "entry_id": completion["id"],
        "run_id": resolved_run, "page_key": resolved_key,
        "result_source": result_source, "result_snapshot_id": result_snapshot_id,
        "action": "complete_retry_page", "timestamp": completion["完成时间"],
        "abnormal": False,
    })
    return {
        "ok": True,
        "message": "补采结果已登记，该页面不再发放执行资格",
        "重复完成": False,
        "结果": completion,
        "剩余工作": _remaining(resolved_run),
        "已补采结果": _completions(resolved_run),
    }


def record_external_completion_for_source(source: str, *, snapshot_id: int | None = None,
                                          snapshot_code: str = "") -> list[dict[str, Any]]:
    """别的入口补采完成时（例如页面快照登记），回写重采工作项。

    以页面地址或页面键匹配尚未补采的工作项，写入补采结果。
    """
    source_text = str(source or "").strip().casefold()
    if not source_text:
        return []
    completed: list[dict[str, Any]] = []
    for item in list(store.rows(ITEM_TABLE)):
        if item.get("状态") == "已补采":
            continue
        candidates = {str(item.get("页面地址") or "").strip().casefold(),
                      str(item.get("页面键") or "").strip().casefold()}
        candidates = {value for value in candidates if value.startswith(("http://", "https://"))}
        if not any(source_text == value or value in source_text for value in candidates):
            continue
        result = complete_page(
            run_id=_as_int(item.get("运行id")),
            page_key_value=str(item.get("页面键")),
            result_summary=f"页面快照{snapshot_code or snapshot_id or ''}补采完成：{source}",
            result_source="页面快照补采",
            result_snapshot_id=snapshot_id,
            actor="页面快照",
        )
        completed.append(result["结果"])
    return completed


def workbench(run_id: int | None = None) -> dict[str, Any]:
    """恢复入口的一次性视图：剩余工作、已补采结果、回执和包。"""
    items = list_items(run_id=run_id)
    receipts = [dict(row) for row in store.rows(RECEIPT_TABLE)
                if run_id is None or _as_int(row.get("运行id")) == run_id]
    packages = [dict(row) for row in store.rows(PACKAGE_TABLE)
                if run_id is None or _as_int(row.get("来源运行id")) == run_id]
    remaining = [item for item in items
                 if item.get("执行资格") and item.get("状态") in ("待补采", "已领取")]
    return {
        "run_id": run_id,
        "剩余工作": remaining,
        "可领取": [item for item in remaining if item.get("状态") == "待补采"],
        "已领取": [item for item in remaining if item.get("状态") == "已领取"],
        "已补采结果": _completions(run_id),
        "工作项": items,
        "回执": receipts,
        "重采输入包": packages,
        "统计": {
            "剩余工作": len(remaining),
            "已补采": sum(1 for item in items if item.get("状态") == "已补采"),
            "包数": len(packages),
            "回执数": len(receipts),
        },
    }


def list_packages(*, run_id: int | None = None, channel_id: int | None = None) -> list[dict[str, Any]]:
    rows = [dict(row) for row in store.rows(PACKAGE_TABLE)]
    if run_id is not None:
        rows = [row for row in rows if _as_int(row.get("来源运行id")) == run_id]
    if channel_id is not None:
        rows = [row for row in rows if _as_int(row.get("通道id")) == channel_id]
    return list(reversed(rows))


def package_download_path(package_id: int) -> dict[str, Any]:
    package = store.find(PACKAGE_TABLE, package_id)
    if package is None:
        raise RetryError(f"重采输入包 {package_id} 不存在")
    path = package.get("文件路径")
    if not path:
        raise RetryError("该重采输入包没有可下载的离线文件")
    if not path or not os.path.exists(path):
        # 文件被清理或服务重启后，用留存的范围和失败明细重建同一份包。
        manifest = package.get("包清单") or {}
        if not manifest:
            raise RetryError("该重采输入包缺少清单，无法重建离线文件")
        failures = failures_for_run(int(package.get("来源运行id") or 0))
        _write_package_file(package, manifest, failures)
        path = package.get("文件路径")
    return package


def _snapshot_completed_elsewhere(entry: dict[str, Any]) -> None:
    """页面快照在别处登记成功时，回写重采工作项，避免同一页重复发放资格。"""
    source = entry.get("采集来源") or entry.get("快照编号") or ""
    code = str(entry.get("快照编号") or "")
    record_external_completion_for_source(
        str(source), snapshot_id=_as_int(entry.get("id")), snapshot_code=code)


register_hook("page_snapshot", "标记存档", _snapshot_completed_elsewhere)
