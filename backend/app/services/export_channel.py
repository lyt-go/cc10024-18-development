"""导出通道领域服务：数据导出、格式转换、分片与归档，消费去重结果和内容缓存。"""
from __future__ import annotations

import json
from typing import Any

from app.store import store
from app.services.base import BaseModuleService
from app import persistence as persist


class ExportChannelService(BaseModuleService):
    MODULE = "export_channel"
    ENTITY = "导出配置"
    OPERATION_TERM = "导出"
    LIST_FIELDS = ["通道编号", "输出格式", "目标路径", "压缩方式", "分片策略", "保留策略", "加密标识", "通道版本"]
    REQUIRED_FIELDS = ["通道编号", "输出格式", "目标路径"]
    STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "启用通道": "已启用", "停用通道": "已停用"}
    NEGATIVE_ACTIONS = ["停用通道"]

    def export_data(self, channel_id: int, data: list[dict[str, Any]]) -> str:
        """通过指定通道导出数据到exports/目录。"""
        channel = store.find(self.MODULE, channel_id)
        if not channel or channel.get("status") != "已启用":
            raise ValueError(f"导出通道{channel_id}未启用或不存在")
        fmt = channel.get("输出格式", "json")
        filename = f"export_{channel_id}_{persist.now_iso().replace(':', '-')}.{fmt}"
        path = persist.data_path("exports", filename)
        if fmt == "json":
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        elif fmt == "csv":
            import csv
            with open(path, "w", newline="", encoding="utf-8") as f:
                if data:
                    writer = csv.DictWriter(f, fieldnames=list(data[0].keys()))
                    writer.writeheader()
                    writer.writerows(data)
        else:
            raise ValueError(f"不支持的输出格式: {fmt}")
        return path

    def list_retry_packages(self) -> list[dict[str, Any]]:
        """导出通道视角的重采输入包交付记录：每批关联原批次、范围和取得时间。"""
        return [
            {
                "通道编号": row.get("通道编号", ""),
                "包编号": row.get("包编号", ""),
                "来源运行id": row.get("来源运行id"),
                "原采集批次id": row.get("原采集批次id"),
                "原采集批次编号": row.get("原采集批次编号", ""),
                "包范围": row.get("包范围", []),
                "页面数": row.get("页面数", 0),
                "取得时间": row.get("取得时间", ""),
                "出库状态": row.get("status", ""),
            }
            for row in store.rows("_retry_package")
        ]

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        channels = store.rows(self.MODULE)
        base["active_channels"] = sum(1 for c in channels if c.get("status") == "已启用")
        # 导出文件列表
        export_files = persist.list_files("exports")
        base["export_files"] = len(export_files)
        # 内容缓存关联
        caches = store.rows("content_cache")
        base["linked_caches"] = len([c for c in caches if c.get("status") == "已生效"])
        # 逐批交付的重采输入包：关联原采集批次、包范围、取得时间
        packages = store.rows("_retry_package")
        base["retry_packages"] = len(packages)
        base["retry_package_batches"] = len({
            p.get("原采集批次id") for p in packages if p.get("原采集批次id") is not None})
        base["retry_package_pages"] = sum(int(p.get("页面数") or 0) for p in packages)
        base["retry_package_latest"] = (
            packages[-1].get("取得时间") if packages else "")
        return base