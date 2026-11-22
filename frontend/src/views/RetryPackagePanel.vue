<template>
  <section class="page retry-panel" data-testid="retry-panel">
    <header class="page-head">
      <div>
        <h2>失败采集重采输入包</h2>
        <p class="page-desc">
          从运行失败采集明细生成可离线核查的压缩包，经导出通道交付；另一班补采后再接收，
          核对可执行范围并只发放一次执行资格。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="retry-refresh" type="button" @click="reload">刷新</button>
        <button class="btn primary" data-testid="retry-export" type="button" :disabled="!runId || !channelId || busy" @click="exportPackage">
          导出重采输入包
        </button>
      </div>
    </header>

    <p v-if="message" class="retry-message" data-testid="retry-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="retry-error">{{ errorMessage }}</p>

    <form class="retry-form" @submit.prevent>
      <label>
        <span>来源运行</span>
        <select v-model.number="runId" data-testid="retry-run-select" @change="loadScope">
          <option :value="0" disabled>选择带失败记录的运行</option>
          <option v-for="run in runs" :key="run.run_id" :value="run.run_id">
            {{ run.记录编号 || `运行 ${run.run_id}` }} · {{ run.关联计划 }} · 失败 {{ run.失败总数 }}
          </option>
        </select>
      </label>
      <label>
        <span>导出通道</span>
        <select v-model.number="channelId" data-testid="retry-channel-select">
          <option v-for="channel in channels" :key="channel.id" :value="channel.id">
            {{ channel.通道编号 }}（{{ channel.status }}）
          </option>
        </select>
      </label>
      <label>
        <span>原采集批次（可选）</span>
        <input v-model="batchId" data-testid="retry-batch-input" placeholder="留空自动从失败明细取" />
      </label>
      <label>
        <span>值班人</span>
        <input v-model="actor" data-testid="retry-actor-input" placeholder="姓名或工号" />
      </label>
      <label>
        <span>班次</span>
        <input v-model="shift" data-testid="retry-shift-input" placeholder="白班 / 夜班" />
      </label>
    </form>

    <section v-if="scope" class="retry-block" data-testid="retry-scope">
      <h3>可重采范围：{{ scope.记录编号 }}（{{ scope.关联计划 }}）</h3>
      <p class="retry-hint">
        失败 {{ scope.失败总数 }} 页，可重采 {{ scope.可重采页面.length }} 页，不可重采
        {{ scope.不可重采页面.length }} 页。
        <span v-if="scope.存量运行" data-testid="retry-legacy-hint">
          存量运行缺少可选字段：{{ scope.缺失可选字段.join('、') }}；仍按页面键与重试边界判断条件重采。
        </span>
      </p>
      <table class="data-table">
        <thead><tr><th>页面键</th><th>失败步骤</th><th>错误类型</th><th>重采条件</th></tr></thead>
        <tbody>
          <tr v-for="page in scope.可重采页面" :key="page.页面键" data-testid="retry-scope-retryable">
            <td>{{ page.页面键 }}</td>
            <td>{{ page.失败步骤 || '—' }}</td>
            <td>{{ page.错误类型 || '—' }}</td>
            <td>{{ page.重采条件 }}</td>
          </tr>
          <tr v-for="page in scope.不可重采页面" :key="page.页面键" class="retry-blocked" data-testid="retry-scope-blocked">
            <td>{{ page.页面键 }}</td>
            <td>{{ page.失败步骤 || '—' }}</td>
            <td>{{ page.错误类型 || '—' }}</td>
            <td>{{ page.不可重采原因 }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="retry-block" data-testid="retry-packages">
      <h3>已出库重采输入包</h3>
      <table class="data-table">
        <thead>
          <tr><th>包编号</th><th>来源运行</th><th>原采集批次</th><th>页面数</th><th>取得时间</th><th>文件</th></tr>
        </thead>
        <tbody>
          <tr v-for="pkg in packages" :key="pkg.id" :data-testid="`retry-package-${pkg.id}`">
            <td>{{ pkg.包编号 }}</td>
            <td>{{ pkg.来源运行记录编号 || pkg.来源运行id }}</td>
            <td>{{ pkg.原采集批次编号 || '未记录' }}</td>
            <td>{{ pkg.页面数 }}</td>
            <td>{{ pkg.取得时间 }}</td>
            <td>
              <a class="link" :href="downloadUrl(pkg.id)" :data-testid="`retry-download-${pkg.id}`">
                下载离线包
              </a>
            </td>
          </tr>
          <tr v-if="!packages.length"><td colspan="6" class="empty-state">还没有出库的重采输入包</td></tr>
        </tbody>
      </table>
    </section>

    <section class="retry-block retry-receive" data-testid="retry-receive">
      <h3>重采输入包接收</h3>
      <p class="retry-hint">打开下载好的离线压缩包核对后，从这里上传同一个 zip 文件；重复上传同一包不会重复发放执行资格。</p>
      <div class="retry-form">
        <label>
          <span>离线压缩包</span>
          <input data-testid="retry-file-input" type="file" accept=".zip,application/zip" @change="onFileChange" />
        </label>
        <button class="btn primary" data-testid="retry-receive-submit" type="button" :disabled="!file || busy" @click="receivePackage">
          {{ busy ? '接收中…' : '接收并核对范围' }}
        </button>
      </div>
      <p v-if="lastReceipt" class="retry-hint" data-testid="retry-last-receipt">
        回执 {{ lastReceipt.回执编号 }} · {{ lastReceipt.重复接收 ? '重复接收' : '首次接收' }} ·
        新增执行资格 {{ lastReceipt.新增执行资格 }} 项 · 原包 {{ lastReceipt.原包范围.length }} 页
      </p>
      <table v-if="lastReceipt" class="data-table" data-testid="retry-receipt-diff">
        <thead><tr><th>差异类型</th><th>页面</th></tr></thead>
        <tbody>
          <tr v-for="(entries, kind) in lastReceipt.差异" :key="kind">
            <td>{{ kind }}</td>
            <td>{{ entries.map((entry) => entry.页面键).join('、') || '—' }}</td>
          </tr>        </tbody>
      </table>
    </section>

    <section class="retry-block" data-testid="retry-external">
      <h3>别处补采结果登记</h3>
      <p class="retry-hint">若某页已在别的入口补采完成，先在这里回填结果；之后接收旧包时该页只留结果、不再发放执行资格。</p>
      <div class="retry-form">
        <label>
          <span>页面键 / 页面地址</span>
          <input v-model="externalPage" data-testid="retry-external-page" placeholder="https://…" />
        </label>
        <label>
          <span>结果摘要</span>
          <input v-model="externalSummary" data-testid="retry-external-summary" placeholder="补采条数或说明" />
        </label>
        <button class="btn" data-testid="retry-external-submit" type="button" :disabled="!externalPage || !externalSummary || busy" @click="registerExternal">
          登记补采结果
        </button>
      </div>
    </section>

    <section class="retry-block" data-testid="retry-workbench">
      <h3>剩余工作与补采结果</h3>
      <div class="retry-stats">
        <span>剩余工作：{{ workbench?.统计.剩余工作 ?? 0 }}</span>
        <span>已补采：{{ workbench?.统计.已补采 ?? 0 }}</span>
        <span>回执：{{ workbench?.统计.回执数 ?? 0 }}</span>
      </div>
      <table class="data-table">
        <thead><tr><th>页面</th><th>失败步骤</th><th>状态</th><th>领取人</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="item in workbench?.剩余工作 ?? []" :key="item.id" :data-testid="`retry-item-${item.id}`">
            <td>{{ item.页面键 }}</td>
            <td>{{ item.失败步骤 || '—' }}</td>
            <td>{{ item.状态 }}</td>
            <td>{{ item.领取人 || '—' }}</td>
            <td class="row-actions">
              <button class="link" :data-testid="`retry-claim-${item.id}`" type="button" @click="claim(item)">领取</button>
            </td>
          </tr>
          <tr v-if="!workbench?.剩余工作?.length">
            <td colspan="5" class="empty-state">没有剩余可领取的重采工作</td>
          </tr>
        </tbody>
      </table>
      <form v-if="workbench?.剩余工作?.length" class="retry-form" @submit.prevent="completeSelected">
        <label>
          <span>要登记结果的工作项</span>
          <select v-model.number="completeItemId" data-testid="retry-complete-item-select">
            <option :value="0" disabled>选择工作项</option>
            <option v-for="item in workbench.剩余工作" :key="item.id" :value="item.id">
              {{ item.页面键 }}（{{ item.状态 }}）
            </option>
          </select>
        </label>
        <label>
          <span>结果摘要</span>
          <input v-model="completeSummary" data-testid="retry-complete-summary" placeholder="补采条数或说明" />
        </label>
        <button class="btn primary" data-testid="retry-complete-submit" type="submit" :disabled="!completeItemId || !completeSummary || busy">
          登记补采结果
        </button>
      </form>
      <table class="data-table">
        <thead><tr><th>已补采页面</th><th>结果摘要</th><th>来源</th><th>完成时间</th></tr></thead>
        <tbody>
          <tr v-for="result in workbench?.已补采结果 ?? []" :key="result.id" :data-testid="`retry-result-${result.id}`">
            <td>{{ result.页面键 }}</td>
            <td>{{ result.结果摘要 }}</td>
            <td>{{ result.结果来源 }}</td>
            <td>{{ result.完成时间 }}</td>
          </tr>
          <tr v-if="!workbench?.已补采结果?.length"><td colspan="4" class="empty-state">还没有补采结果</td></tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import type { EntryRow } from '@/data/types'
import {
  claimRetryItem,
  completeRetryItem,
  completeRetryPage,
  createRetryPackage,
  getRetryWorkbench,
  getRunScope,
  listRetryPackages,
  listRetryRuns,
  receiveRetryPackage,
  retryPackageDownloadUrl,
} from '@/api/retry-api'
import { listEntries } from '@/api/backend-service'
import type {
  RetryReceipt,
  RetryWorkbench,
  RunScope,
  RetryPackage,
  RetryItem,
} from '@/api/retry-api'

const props = defineProps<{ moduleKey?: string }>()
// 面板挂在错误恢复页，moduleKey 由工作台透传，避免模板未使用告警。
void props

const runs = ref<RunScope[]>([])
const runId = ref(0)
const channelId = ref(0)
const channels = ref<EntryRow[]>([])
const batchId = ref('')
const actor = ref('')
const shift = ref('')
const scope = ref<RunScope | null>(null)
const packages = ref<RetryPackage[]>([])
const workbench = ref<RetryWorkbench | null>(null)
const lastReceipt = ref<RetryReceipt | null>(null)
const file = ref<File | null>(null)
const externalPage = ref('')
const externalSummary = ref('')
const completeItemId = ref(0)
const completeSummary = ref('')
const busy = ref(false)
const message = ref('')
const errorMessage = ref('')

function downloadUrl(packageId: number): string {
  return retryPackageDownloadUrl(packageId)
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  file.value = input.files?.[0] ?? null
}

async function loadChannels() {
  const payload = await listEntries('export_channel')
  channels.value = payload.items
  if (!channelId.value) {
    const enabled = payload.items.find((row) => row.status === '已启用')
    channelId.value = Number(enabled?.id ?? payload.items[0]?.id ?? 0)
  }
}

async function loadScope() {
  if (!runId.value) return
  try {
    scope.value = await getRunScope(runId.value)
  } catch (error) {
    scope.value = null
    errorMessage.value = error instanceof Error ? error.message : '读取失败范围失败'
  }
}

async function loadPackages() {
  const payload = await listRetryPackages()
  packages.value = payload.items
}

async function loadWorkbench() {
  workbench.value = await getRetryWorkbench(runId.value || undefined)
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listRetryRuns()
    runs.value = payload.items
    if (!runId.value && payload.items.length) {
      runId.value = payload.items[0].run_id
    }
    await loadChannels()
    await loadScope()
    await loadPackages()
    await loadWorkbench()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '读取重采输入包失败'
  }
}

async function exportPackage() {
  errorMessage.value = ''
  message.value = ''
  busy.value = true
  try {
    const result = await createRetryPackage({
      run_id: runId.value,
      channel_id: channelId.value,
      batch_id: batchId.value ? Number(batchId.value) : null,
      actor: actor.value,
      shift: shift.value,
    })
    message.value = result.message
    await loadPackages()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  } finally {
    busy.value = false
  }
}

async function receivePackage() {
  if (!file.value) return
  errorMessage.value = ''
  message.value = ''
  busy.value = true
  try {
    const result = await receiveRetryPackage(file.value, actor.value, shift.value)
    message.value = result.message
    lastReceipt.value = result.回执
    runId.value = result.回执.运行id
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '接收失败'
  } finally {
    busy.value = false
  }
}

async function registerExternal() {
  errorMessage.value = ''
  message.value = ''
  busy.value = true
  try {
    const result = await completeRetryPage({
      run_id: runId.value || undefined,
      page_address: externalPage.value,
      result_summary: externalSummary.value,
      result_source: '别处补采',
      actor: actor.value,
      shift: shift.value,
    })
    message.value = result.message
    externalSummary.value = ''
    await loadWorkbench()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    busy.value = false
  }
}

async function claim(item: RetryItem) {
  errorMessage.value = ''
  try {
    const result = await claimRetryItem(item.id, actor.value, shift.value)
    message.value = result.message
    await loadWorkbench()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '领取失败'
  }
}

async function completeSelected() {
  if (!completeItemId.value || !completeSummary.value) return
  errorMessage.value = ''
  message.value = ''
  busy.value = true
  try {
    const result = await completeRetryItem(completeItemId.value, completeSummary.value, actor.value, shift.value)
    message.value = result.message
    completeSummary.value = ''
    completeItemId.value = 0
    await loadWorkbench()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    busy.value = false
  }
}

onMounted(reload)
</script>

