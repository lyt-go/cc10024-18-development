"""失败采集重采输入包测试：可选字段缺失、出库关联、离线包接收、幂等与补采结果。"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import zipfile

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app import persistence as persist
from app.services import retry
from app.services.export_channel import ExportChannelService


def _reset_state() -> None:
    for sub in ("exports", "runs"):
        path = persist.data_path(sub, "")
        if os.path.isdir(path):
            for name in os.listdir(path):
                os.remove(os.path.join(path, name))
    for table in (retry.PACKAGE_TABLE, retry.ITEM_TABLE,
                  retry.RECEIPT_TABLE, retry.COMPLETION_TABLE):
        retry.store.rows(table).clear()


def _open_package(path: str) -> tuple[dict, list[dict]]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        assert {"manifest.json", "failures.json", "README.txt"} <= names
        manifest = json.loads(archive.read("manifest.json"))
        failures = json.loads(archive.read("failures.json"))
    return manifest, failures["记录"]


def test_legacy_scope_without_optional_fields():
    """存量运行没有页面地址/采集批次/可重采字段时仍能给出条件重采页面。"""
    scope = retry.run_failure_scope(3)
    assert scope["失败总数"] == 2
    assert scope["存量运行"] is True
    assert "页面地址" in scope["缺失可选字段"]
    keys = [page["页面键"] for page in scope["可重采页面"]]
    assert keys == ["https://demo.example.com/legacy/7", "https://demo.example.com/legacy/8"]
    assert all(page["重采条件"] for page in scope["可重采页面"])


def test_scope_by_plan_only_record():
    """只有关联计划、没有运行 id 的明细也归到对应运行。"""
    scope = retry.run_failure_scope(2)
    assert scope["失败总数"] == 1
    assert scope["可重采页面"][0]["页面键"] == "https://demo.example.com/legacy-plan/5"


def test_package_export_and_batch_linkage():
    """出库包带原采集批次、包范围与取得时间，并排除不可重采页面。"""
    _reset_state()
    package = retry.create_package(run_id=1, channel_id=3, actor="交班人", shift="白班")
    assert package["原采集批次id"] == 2
    assert package["原采集批次编号"] == "BATC-0002"
    assert package["页面数"] == 3
    assert "https://demo.example.com/removed/9" in package["不可执行页面"]
    assert package["取得时间"]
    assert os.path.exists(package["文件路径"])

    linkage = ExportChannelService().list_retry_packages()
    assert len(linkage) == 1
    assert linkage[0]["原采集批次id"] == 2
    assert len(linkage[0]["包范围"]) == 3
    assert linkage[0]["取得时间"] == package["取得时间"]

    snapshot = ExportChannelService().domain_snapshot()
    assert snapshot["retry_packages"] == 1
    assert snapshot["retry_package_pages"] == 3

    manifest, failures = _open_package(package["文件路径"])
    assert manifest["包编号"] == package["包编号"]
    assert manifest["内容指纹"] == package["内容指纹"]
    assert len(failures) == 4  # 原始失败记录完整入包，含不可重采页


def test_disabled_channel_rejected():
    _reset_state()
    try:
        retry.create_package(run_id=1, channel_id=1)
    except retry.RetryError as exc:
        assert "未启用" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("未启用通道不应出包")


def test_receive_skips_completed_pages_and_duplicate_receipt():
    """另一班补回两页后接收旧包：只发剩余资格；重复接收不再发资格。"""
    _reset_state()
    package = retry.create_package(run_id=1, channel_id=3)
    with open(package["文件路径"], "rb") as handle:
        raw = handle.read()

    retry.complete_page(run_id=1, page_address_value="https://demo.example.com/detail/42",
                        result_summary="另一班补采42")
    retry.complete_page(run_id=1, page_address_value="https://demo.example.com/detail/43",
                        result_summary="另一班补采43")

    first = retry.receive_package(raw=raw, filename="pkg.zip")
    assert first["重复接收"] is False
    assert first["新增执行资格"] == 1
    assert [item["页面键"] for item in first["剩余工作"]] == [
        "https://demo.example.com/list?page=1"]
    skipped = {entry["页面键"] for entry in first["差异"]["已补采跳过"]}
    assert skipped == {"https://demo.example.com/detail/42",
                       "https://demo.example.com/detail/43"}

    second = retry.receive_package(raw=raw, filename="pkg.zip")
    assert second["重复接收"] is True
    assert second["新增执行资格"] == 0
    assert second["差异"]["新增可执行"] == []
    assert [item["页面键"] for item in second["剩余工作"]] == [
        "https://demo.example.com/list?page=1"]

    completions = {item["页面键"]: item for item in retry.workbench(1)["已补采结果"]}
    assert completions["https://demo.example.com/detail/42"]["结果摘要"] == "另一班补采42"
    assert completions["https://demo.example.com/detail/43"]["结果摘要"] == "另一班补采43"


def test_claim_and_complete_remaining_work():
    _reset_state()
    package = retry.create_package(run_id=1, channel_id=3)
    with open(package["文件路径"], "rb") as handle:
        raw = handle.read()
    retry.receive_package(raw=raw)

    remaining = retry.workbench(1)["剩余工作"]
    assert len(remaining) == 3
    item = remaining[0]
    claimed = retry.claim_item(item["id"], actor="交班人", shift="白班")
    assert claimed["状态"] == "已领取"
    try:
        retry.claim_item(item["id"], actor="第三班")
    except retry.RetryError as exc:
        assert "已被" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("同一页不应被两人同时领取")

    result = retry.complete_item(item["id"], result_summary="补采成功")
    assert result["重复完成"] is False
    assert len(result["剩余工作"]) == 2
    repeat = retry.complete_item(item["id"], result_summary="再补一次")
    assert repeat["重复完成"] is True

    # 同一页补采后再接收同一包，也不会给该页新资格。
    again = retry.receive_package(raw=raw)
    assert again["新增执行资格"] == 0
    assert all(entry["页面键"] != item["页面键"]
               for entry in again["差异"]["新增可执行"])


def test_tampered_package_rejected():
    _reset_state()
    package = retry.create_package(run_id=1, channel_id=3)
    with zipfile.ZipFile(package["文件路径"]) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        failures = json.loads(archive.read("failures.json"))
    failures["记录"].append({"id": 999, "运行id": 1, "页面键": "https://x/extra",
                             "失败步骤": "data_extract", "错误类型": "超时"})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr("failures.json", json.dumps(failures, ensure_ascii=False))
    try:
        retry.receive_package(raw=buffer.getvalue())
    except retry.RetryError as exc:
        assert "指纹" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("被篡改的包应拒绝接收")


def test_snapshot_hook_completes_page():
    """页面快照在别处补采完成时，回写工作项并停止发放资格。"""
    _reset_state()
    package = retry.create_package(run_id=1, channel_id=3)
    with open(package["文件路径"], "rb") as handle:
        raw = handle.read()
    retry.receive_package(raw=raw)

    completed = retry.record_external_completion_for_source(
        "https://demo.example.com/detail/42", snapshot_id=77, snapshot_code="PAGE-TEST")
    assert len(completed) == 1
    assert completed[0]["结果来源"] == "页面快照补采"
    assert completed[0]["结果快照id"] == 77

    remaining = [item["页面键"] for item in retry.workbench(1)["剩余工作"]]
    assert "https://demo.example.com/detail/42" not in remaining


def test_workbench_stats_and_receipts():
    _reset_state()
    package = retry.create_package(run_id=1, channel_id=3)
    with open(package["文件路径"], "rb") as handle:
        raw = handle.read()
    retry.receive_package(raw=raw)
    retry.receive_package(raw=raw)
    view = retry.workbench(1)
    assert view["统计"] == {"剩余工作": 3, "已补采": 0, "包数": 1, "回执数": 2}
    assert [row["重复接收"] for row in view["回执"]] == [False, True]


def test_completion_repairs_stale_item_state():
    """已补采结果先到、工作项后建时，工作项状态与执行资格保持一致。"""
    _reset_state()
    retry.complete_page(run_id=1, page_key_value="https://demo.example.com/detail/42",
                        result_summary="先补采")
    package = retry.create_package(run_id=1, channel_id=3)
    with open(package["文件路径"], "rb") as handle:
        raw = handle.read()
    first = retry.receive_package(raw=raw)
    assert first["新增执行资格"] == 2
    item = retry._find_item(1, "https://demo.example.com/detail/42")
    assert item["状态"] == "已补采"
    assert item["执行资格"] is False
    assert item["结果摘要"] == "先补采"
    # 之后重复登记同一页：不发新资格，也不篡改已有工作项。
    repeat = retry.complete_page(run_id=1, page_key_value="https://demo.example.com/detail/42",
                                 result_summary="再补一次")
    assert repeat["重复完成"] is True
    assert item["结果摘要"] == "先补采"
    assert item["执行资格"] is False


def test_completion_address_mirrored_for_direct_key():
    """按页面键直接补采时，页面地址留给地址核对使用。"""
    _reset_state()
    package = retry.create_package(run_id=1, channel_id=3)
    with open(package["文件路径"], "rb") as handle:
        raw = handle.read()
    retry.receive_package(raw=raw)
    result = retry.complete_page(run_id=1, page_key_value="https://demo.example.com/list?page=1",
                                 result_summary="按页面键补采")
    assert result["结果"]["页面地址"] == "https://demo.example.com/list?page=1"


def test_download_rebuilds_missing_file():
    """离线文件被清理后，下载会用留存清单重建同一份包。"""
    _reset_state()
    package = retry.create_package(run_id=1, channel_id=3)
    os.remove(package["文件路径"])
    try:
        retry.package_download_path(package["id"])
    except retry.RetryError as exc:  # pragma: no cover - 兼容旧包
        raise AssertionError(f"不应失败: {exc}")
    assert os.path.exists(package["文件路径"])
    manifest, failures = _open_package(package["文件路径"])
    assert manifest["内容指纹"] == package["内容指纹"]
    assert len(failures) == 4



def test_external_completion_requires_exact_page():
    """页面快照补采只回写实际匹配的页面，不会把空地址当通配命中其他页。"""
    _reset_state()
    package = retry.create_package(run_id=3, channel_id=3)
    with open(package["文件路径"], "rb") as handle:
        raw = handle.read()
    retry.receive_package(raw=raw)
    # legacy 记录没有页面地址，其他来源不应误伤。
    assert retry.record_external_completion_for_source("PAGE-0003") == []
    assert len(retry.workbench(3)["剩余工作"]) == 2
    completed = retry.record_external_completion_for_source(
        "https://demo.example.com/legacy/7", snapshot_id=9, snapshot_code="PAGE-9")
    assert len(completed) == 1
    remaining = [item["页面键"] for item in retry.workbench(3)["剩余工作"]]
    assert remaining == ["https://demo.example.com/legacy/8"]


if __name__ == "__main__":
    _tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for _test in _tests:
        _test()
        print(f"PASS: {_test.__name__}")
    print(f"\nALL {len(_tests)} RETRY TESTS PASSED")
