/** 失败采集重采输入包接口：导出、下载、离线包接收、领取与补采结果。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export type RetryPage = {
  页面键: string
  页面地址?: string
  失败步骤?: string
  错误类型?: string
  重试边界?: string
  重采条件?: string
  不可重采原因?: string
  失败记录id?: number
}

export type RunScope = {
  run_id: number
  记录编号: string
  关联计划: string
  失败总数: number
  可重采页面: RetryPage[]
  不可重采页面: RetryPage[]
  缺失可选字段: string[]
  存量运行: boolean
}

export type RetryPackage = {
  id: number
  包编号: string
  来源运行id: number
  来源运行记录编号: string
  原采集批次id: number | null
  原采集批次编号: string
  通道编号: string
  取得时间: string
  页面数: number
  包范围: string[]
  不可执行页面: string[]
  文件名: string
  status: string
}

export type RetryItem = {
  id: number
  包编号: string
  运行id: number
  页面键: string
  页面地址: string
  失败步骤: string
  状态: string
  执行资格: boolean
  领取人: string
  结果摘要: string
  结果来源: string
  完成时间: string
}

export type RetryCompletion = {
  id: number
  结果编号: string
  运行id: number
  页面键: string
  结果摘要: string
  结果来源: string
  完成时间: string
}

export type ReceiptDiffEntry = { 页面键: string; [detail: string]: unknown }

export type RetryReceipt = {
  id: number
  回执编号: string
  包编号: string
  运行id: number
  接收时间: string
  重复接收: boolean
  新增执行资格: number
  原包范围: string[]
  当前可执行范围: string[]
  差异: Record<string, ReceiptDiffEntry[]>
}

export type RetryWorkbench = {
  剩余工作: RetryItem[]
  可领取: RetryItem[]
  已领取: RetryItem[]
  已补采结果: RetryCompletion[]
  工作项: RetryItem[]
  回执: RetryReceipt[]
  重采输入包: RetryPackage[]
  统计: { 剩余工作: number; 已补采: number; 包数: number; 回执数: number }
}

export type ReceiveResult = {
  ok: boolean
  message: string
  重复接收: boolean
  新增执行资格: number
  回执编号: string
  回执: RetryReceipt
  包: RetryPackage
  差异: Record<string, { 页面键: string }[]>
  剩余工作: RetryItem[]
  已补采结果: RetryCompletion[]
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init)
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    throw new Error(detail?.detail ?? `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

export function listRetryRuns(): Promise<{ items: RunScope[] }> {
  return json('/api/retry/runs')
}

export function getRunScope(runId: number): Promise<RunScope> {
  return json(`/api/retry/runs/${runId}/scope`)
}

export function listRetryPackages(runId?: number): Promise<{ items: RetryPackage[] }> {
  const query = runId ? `?run_id=${runId}` : ''
  return json(`/api/retry/packages${query}`)
}

export function createRetryPackage(payload: {
  run_id: number
  channel_id: number
  batch_id?: number | null
  actor?: string
  shift?: string
}): Promise<{ ok: boolean; message: string; package: RetryPackage }> {
  return json('/api/retry/packages', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export function retryPackageDownloadUrl(packageId: number): string {
  return `${API_BASE}/api/retry/packages/${packageId}/download`
}

export async function receiveRetryPackage(
  file: File,
  actor: string,
  shift: string,
): Promise<ReceiveResult> {
  const form = new FormData()
  form.append('file', file)
  form.append('actor', actor)
  form.append('shift', shift)
  return json<ReceiveResult>('/api/retry/receive', { method: 'POST', body: form })
}

export function getRetryWorkbench(runId?: number): Promise<RetryWorkbench> {
  const query = runId ? `?run_id=${runId}` : ''
  return json(`/api/retry/workbench${query}`)
}

export function claimRetryItem(itemId: number, actor: string, shift: string): Promise<{ ok: boolean; message: string; item: RetryItem }> {
  return json(`/api/retry/items/${itemId}/claim`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ actor, shift }),
  })
}

export function completeRetryItem(
  itemId: number,
  resultSummary: string,
  actor: string,
  shift: string,
): Promise<{ ok: boolean; message: string; 重复完成: boolean }> {
  return json(`/api/retry/items/${itemId}/complete`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ result_summary: resultSummary, actor, shift }),
  })
}

export function completeRetryPage(payload: {
  run_id?: number
  page_key?: string
  page_address?: string
  result_summary: string
  result_source?: string
  result_snapshot_id?: number | null
  actor?: string
  shift?: string
}): Promise<{ ok: boolean; message: string; 重复完成: boolean }> {
  return json('/api/retry/completions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export type ExportPackageRow = {
  通道编号: string
  包编号: string
  来源运行id: number
  原采集批次id: number | null
  原采集批次编号: string
  包范围: string[]
  页面数: number
  取得时间: string
  出库状态: string
}
