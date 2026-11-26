<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <form v-if="supportsEligibility" class="filter-bar eligibility-bar" @submit.prevent="lookupBatch">
      <label class="filter-item">
        <span>按批次回查可执行申请</span>
        <input v-model="batchQuery" data-testid="batch-query" placeholder="输入批次编号，如 007" />
      </label>
      <button class="btn" data-testid="lookup-batch" type="submit">回查申请</button>
      <span v-if="batchMessage" class="hint-text" data-testid="batch-message">{{ batchMessage }}</span>
    </form>

    <section v-if="batchApplications.length" class="detail-card" data-testid="batch-applications">
      <h3>批次 {{ batchQuery || '全部' }} 的差异申请</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>差异编号</th><th>版本对</th><th>可执行</th><th>重解析单</th><th>回执结论</th><th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="app in batchApplications"
            :key="app.差异编号"
            :data-testid="`application-${app.差异编号}`"
          >
            <td>{{ app.差异编号 }}</td>
            <td>{{ app.旧模板版本 }}→{{ app.新模板版本 }}</td>
            <td>{{ app.可执行 ? '可执行' : '不可执行' }}</td>
            <td>{{ app.重解析任务编号 || '—' }}</td>
            <td>{{ app.范围校验回执?.结论 ?? '—' }}</td>
            <td>{{ app.拒绝原因 || app.零变化结论 || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <template v-if="allowedActions(row).length">
              <button
                v-for="action in allowedActions(row)"
                :key="action"
                class="link"
                type="button"
                :data-testid="`action-${row.id}-${action}`"
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
            </template>
            <span v-else class="muted-text">不可执行</span>
            <button
              class="link detail-link"
              type="button"
              :data-testid="`detail-${row.id}`"
              @click="openDetail(row)"
            >
              详情
            </button>
          </td>
        </tr>
        <tr v-if="rows.length" class="hint-row">
          <td :colspan="meta.fields.length + 2" class="hint-text">{{ rowHint }}</td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <section v-if="detail" class="detail-card" data-testid="detail-panel">
      <header class="detail-head">
        <h3>{{ meta.entity }}详情 · {{ detail.entry[meta.fields[0]] ?? detail.entry.id }}</h3>
        <button class="btn ghost" type="button" data-testid="close-detail" @click="detail = null">关闭</button>
      </header>
      <dl class="detail-grid">
        <template v-for="column in meta.fields" :key="column">
          <dt>{{ column }}</dt>
          <dd>{{ detail.entry[column] ?? '—' }}</dd>
        </template>
        <dt>资格结论</dt>
        <dd data-testid="detail-eligibility">{{ eligibilityText }}</dd>
        <dt>比较结论</dt>
        <dd data-testid="detail-conclusion">{{ comparisonConclusion }}</dd>
        <dt>范围校验回执</dt>
        <dd data-testid="detail-receipt">{{ receiptText }}</dd>
        <dt>处理结果 / 产物</dt>
        <dd data-testid="detail-artifact">{{ artifactText }}</dd>
      </dl>
      <p v-if="detailError" class="error-text">{{ detailError }}</p>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  loadBatchEligibility,
  loadEntryDetail,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type {
  EligibilityApplication,
  EntryDetail,
  EntryRow,
} from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const supportsEligibility = computed(() => meta.key === 'tmpl_diff')
const batchQuery = ref('')
const batchApplications = ref<EligibilityApplication[]>([])
const batchMessage = ref('')
const detail = ref<EntryDetail | null>(null)
const detailError = ref('')
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

const rowHint = computed(() => {
  const locked = rows.value.filter((row) => !allowedActions(row).length).length
  if (!locked) return '所有条目当前均可执行本模块动作'
  return `${locked} 条记录没有可执行动作，详情里保留了拒绝原因与比较结论`
})

const eligibilityText = computed(() => {
  const extra = detail.value?.extra
  const conclusion = extra?.资格结论
  if (conclusion) {
    if (conclusion.可执行) return '可执行：已取得范围校验回执'
    return `不可执行：${conclusion.拒绝原因 || '未取得执行回执'}`
  }
  if (extra?.可执行 === false) return `不可执行：${extra.拒绝原因 || '未取得执行回执'}`
  if (extra?.可执行 === true) return '可执行：已取得范围校验回执'
  return detail.value?.entry.资格说明 || '本条目没有额外资格限制'
})

const comparisonConclusion = computed(() => {
  const extra = detail.value?.extra
  return (
    extra?.资格结论?.比较结论 ||
    extra?.资格结论?.零变化结论 ||
    extra?.处理结果 ||
    '暂无比较结论'
  )
})

const receiptText = computed(() => {
  const receipt = detail.value?.extra?.范围校验回执
  if (!receipt) return '没有范围校验回执'
  const fields = receipt.命中字段 ? `，命中字段 ${receipt.命中字段}` : ''
  const batches = receipt.验证批次?.length ? `，验证批次 ${receipt.验证批次.join(',')}` : ''
  const reason = receipt.原因 ? `，原因 ${receipt.原因}` : ''
  return `${receipt.结论 ?? '—'}（${receipt.回执编号 ?? '无编号'}${fields}${batches}${reason}）`
})

const artifactText = computed(() => {
  const extra = detail.value?.extra
  const executed = extra?.执行回执
  const result = extra?.处理结果
  if (result) return result
  if (executed?.处理结论) return executed.处理结论
  return '尚无已发布的处理产物；已确认的送达内容仍可在结果投递中核对'
})

function allowedActions(row: EntryRow): string[] {
  if (Array.isArray(row.可执行动作)) return row.可执行动作
  return meta.actions
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
    if (detail.value && Number(detail.value.entry.id) === Number(row.id)) {
      await openDetail(row)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function openDetail(row: EntryRow) {
  detailError.value = ''
  try {
    detail.value = await loadEntryDetail(meta.key, Number(row.id), batchQuery.value)
  } catch (error) {
    detailError.value = error instanceof Error ? error.message : '比较详情读取失败'
  }
}

async function lookupBatch() {
  batchMessage.value = ''
  errorMessage.value = ''
  try {
    const payload = await loadBatchEligibility(meta.key, batchQuery.value)
    batchApplications.value = payload.applications
    const runnable = payload.applications.filter((app) => app.可执行).length
    batchMessage.value = payload.applications.length
      ? `共 ${payload.applications.length} 份申请，其中 ${runnable} 份仍可执行`
      : '该批次没有可回查的差异申请'
  } catch (error) {
    batchApplications.value = []
    errorMessage.value = error instanceof Error ? error.message : '按批次回查申请失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
