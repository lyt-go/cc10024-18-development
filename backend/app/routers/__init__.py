"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, retry

# 重采输入包路由必须先注册：/api/retry/... 的固定路径要抢在
# 通用资源路由 /api/{module}/{entry_id} 之前匹配，否则会被当成模块 id 解析。
ROUTERS = [retry, resource]
