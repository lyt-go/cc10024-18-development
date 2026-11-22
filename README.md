# 网页行为采集与内容清洗平台

面向网页行为录制、脚本化采集、内容清洗与结构化输出的数据采集与加工平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 行为脚本 | `behavior_script` | 行为脚本 | 脚本编号、目标站点、录制步骤 |
| 元素定位 | `element_selector` | 元素锚点 | 锚点编号、关联脚本、选择器类型 |
| 数据抽取 | `data_extract` | 抽取模板 | 模板编号、关联脚本、抽取字段 |
| 清洗管道 | `clean_pipeline` | 清洗工序 | 工序编号、输入源、清洗规则 |
| 转换规则 | `transform_rule` | 转换规则 | 规则编号、关联工序、源字段 |
| 质量防护 | `quality_guard` | 质量规则 | 规则编号、检核对象、检核维度 |
| 脚本调度 | `script_schedule` | 调度计划 | 计划编号、关联脚本、触发条件 |
| 登录凭证 | `login_mgr` | 登录凭证 | 凭证编号、目标站点、认证方式 |
| 页面快照 | `page_snapshot` | 页面快照 | 快照编号、采集来源、页面版本 |
| 错误恢复 | `error_recover` | 恢复任务 | 任务编号、关联计划、失败步骤 |
| 页面去重 | `page_dedup` | 去重规则 | 规则编号、去重维度、特征字段 |
| 字段归一化 | `field_normalize` | 归一规则 | 规则编号、关联字段、归一类型 |
| 批次队列 | `batch_queue` | 采集批次 | 批次编号、关联计划、任务数量 |
| 画布配置 | `canvas_config` | 画布配置 | 配置编号、关联站点、分辨率 |
| 导出通道 | `export_channel` | 导出配置 | 通道编号、输出格式、目标路径 |
| 运行历史 | `run_history` | 运行记录 | 记录编号、关联计划、开始时间 |
| Schema校验 | `schema_check` | Schema规则 | 规则编号、目标数据、字段名 |
| 内容缓存 | `content_cache` | 缓存配置 | 配置编号、缓存类型、过期策略 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 失败采集重采输入包

交班时可以把一次运行里的原始失败采集记录导出成离线 zip，交给另一值班人核查，
之后从恢复入口上传同一文件重新导入。相关实现：

- `backend/app/services/retry.py`：失败明细范围判定、出库、接收、领取和补采结果。
- `backend/app/routers/retry.py`：`/api/retry/*` 接口（不占用通用模块表）。
- `frontend/src/views/RetryPackagePanel.vue`：错误恢复页下方的重采输入包面板。

要点：

- 导出只走 `已启用` 的导出配置通道，登记原采集批次、包范围与取得时间；
  关联体现在 `GET /api/retry/export-linkage` 和导出通道的 `domain_snapshot` 里。
- 存量运行缺少 `页面地址`、`采集批次id`、`可重采` 等可选字段时，
  仍按错误类型与重试边界判定条件重采页面（`GET /api/retry/runs/{id}/scope`）。
- 接收包按 `内容指纹` 幂等：重复接收同一包、或某页已有补采结果时，
  不会为该页新建执行资格；回执记录原包范围与当前可执行范围的差异。
- 同一运行同一页的补采结果只保留一份，可在 `GET /api/retry/results` 核对。
- 页面快照“标记存档”如果对应某个待补采页面，会自动回写补采结果并停止发资格。

典型流程：

```bash
# 1. 出包（也可用 GET /api/retry/runs/{run_id}/export?... 直接下载）
curl -X POST http://127.0.0.1:8000/api/retry/packages \
  -H 'Content-Type: application/json' \
  -d '{"run_id":1,"channel_id":3,"actor":"交班人","shift":"白班"}'
curl -o pkg.zip http://127.0.0.1:8000/api/retry/packages/1/download

# 2. 另一班在别处补回其中两页后，交班人上传旧包
curl -X POST http://127.0.0.1:8000/api/retry/receive -F file=@pkg.zip

# 3. 查看剩余工作与已补采结果
curl 'http://127.0.0.1:8000/api/retry/workbench?run_id=1'
```

前端在“错误恢复”页底部提供同样的操作：导出、下载、上传接收、领取与登记补采结果。

## 约定

领域：数据工程
子方向：数据采集、爬虫与RPA
