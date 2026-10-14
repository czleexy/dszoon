<template>
  <div>
    <el-card shadow="never" class="page-card">
      <div class="toolbar">
        <el-button type="primary" :disabled="!canWrite" @click="openCreate">
          <el-icon><Plus /></el-icon>新增策略
        </el-button>
        <div class="spacer" />
        <el-text type="info" size="small">
          错峰抖动只影响"策略什么时候发起"；设备之间的分散由全局并发上限与同设备串行保证
        </el-text>
      </div>

      <el-table v-loading="loading" :data="schedules">
        <el-table-column prop="name" label="名称" min-width="160" />
        <el-table-column label="计划" min-width="220">
          <template #default="{ row }">
            <div>{{ row.description }}</div>
            <el-text type="info" size="small">
              {{ row.kind === 'cron' ? `cron: ${row.cron}` : `间隔: ${row.interval_minutes} 分钟` }}
            </el-text>
          </template>
        </el-table-column>
        <el-table-column label="范围" min-width="180">
          <template #default="{ row }">
            <el-text size="small">{{ scopeText(row) }}</el-text>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="90">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              :disabled="!canWrite"
              @change="(value) => toggle(row, value)"
            />
          </template>
        </el-table-column>
        <el-table-column label="下次执行" width="170">
          <template #default="{ row }">{{ formatTime(row.next_run_at) }}</template>
        </el-table-column>
        <el-table-column label="上次结果" width="150">
          <template #default="{ row }">
            <el-tag v-if="row.last_status" size="small" :type="lastStatusType(row.last_status)">
              {{ lastStatusLabel(row.last_status) }}
            </el-tag>
            <span v-else>-</span>
            <div v-if="row.last_run_at" class="sub">{{ formatTime(row.last_run_at) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="290">
          <template #default="{ row }">
            <el-button link type="primary" size="small" :disabled="!canWrite" @click="runNow(row)">
              立即执行
            </el-button>
            <el-button link type="primary" size="small" @click="openPreview(row)">预览</el-button>
            <el-button link type="primary" size="small" @click="openHistory(row)">任务历史</el-button>
            <el-button link type="primary" size="small" :disabled="!canWrite" @click="openEdit(row)">
              编辑
            </el-button>
            <el-button link type="danger" size="small" :disabled="!isAdmin" @click="remove(row)">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="formVisible" :title="form.id ? '编辑策略' : '新增策略'" width="680px">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="110px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" maxlength="128" />
        </el-form-item>

        <el-form-item label="调度方式">
          <el-radio-group v-model="form.kind">
            <el-radio-button value="cron">cron 表达式</el-radio-button>
            <el-radio-button value="interval">固定间隔</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="form.kind === 'cron'" label="cron" prop="cron">
          <el-input v-model="form.cron" placeholder="分 时 日 月 周，例如 0 2 * * *">
            <template #append>
              <el-button @click="form.cron = '0 2 * * *'">每天 02:00</el-button>
            </template>
          </el-input>
          <el-text type="info" size="small">左到右：分、时、日、月、周；时间按服务器本地时区</el-text>
        </el-form-item>

        <el-form-item v-else label="间隔（分钟）" prop="interval_minutes">
          <el-input-number v-model="form.interval_minutes" :min="1" :max="43200" />
        </el-form-item>

        <el-form-item label="错峰抖动">
          <el-input-number v-model="form.jitter_minutes" :min="0" :max="120" />
          <el-text type="info" size="small" style="margin-left: 8px">
            分钟；实际发起时刻在计划之后随机推迟（不会提前），避免多个策略在同一刻一起动手
          </el-text>
        </el-form-item>

        <el-form-item label="执行窗口">
          <el-time-picker
            v-model="windowRange"
            is-range
            format="HH:mm"
            value-format="HH:mm"
            placeholder="不限窗口（全天可执行）"
            style="width: 320px"
          />
          <el-text type="info" size="small" style="margin-left: 8px">可跨零点，如 22:00–06:00</el-text>
        </el-form-item>

        <el-divider content-position="left">备份范围</el-divider>
        <el-form-item label="范围类型">
          <el-radio-group v-model="form.scopeType">
            <el-radio-button value="all">全部启用设备</el-radio-button>
            <el-radio-button value="group">按分组</el-radio-button>
            <el-radio-button value="site">按站点</el-radio-button>
            <el-radio-button value="vendor">按厂商</el-radio-button>
            <el-radio-button value="devices">指定设备</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="form.scopeType === 'group'" label="分组">
          <el-select v-model="form.group_id" placeholder="选择分组" style="width: 320px">
            <el-option
              v-for="group in groups"
              :key="group.id"
              :label="`${group.name}（${group.device_count} 台）`"
              :value="group.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item v-if="form.scopeType === 'site'" label="站点">
          <el-input v-model="form.site" style="width: 320px" placeholder="与设备台账里的站点完全一致" />
        </el-form-item>

        <el-form-item v-if="form.scopeType === 'vendor'" label="厂商">
          <el-select v-model="form.vendor" style="width: 320px">
            <el-option
              v-for="item in vendors"
              :key="item.name"
              :label="item.display_name"
              :value="item.name"
            />
          </el-select>
        </el-form-item>

        <el-form-item v-if="form.scopeType === 'devices'" label="设备">
          <el-select v-model="form.device_ids" multiple filterable style="width: 100%">
            <el-option
              v-for="device in devices"
              :key="device.id"
              :label="`${device.name}（${device.host}）`"
              :value="device.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="包含停用设备">
          <el-switch v-model="form.include_disabled" />
        </el-form-item>

        <el-divider content-position="left">采集参数</el-divider>
        <el-form-item label="预检">
          <el-switch v-model="form.precheck" />
          <el-text type="info" size="small" style="margin-left: 8px">
            先探测设备可达性，减少无效的完整采集
          </el-text>
        </el-form-item>
        <el-form-item label="失败重试">
          <el-input-number v-model="form.retries" :min="0" :max="5" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitForm">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="previewVisible" title="接下来 5 次执行时刻" width="460px">
      <el-alert
        v-if="previewJitter"
        type="info"
        :closable="false"
        show-icon
        title="该策略设置了错峰抖动，每次计算都会重新抖动，所以预览是「大概的时间范围」"
        style="margin-bottom: 12px"
      />
      <el-timeline>
        <el-timeline-item v-for="(time, index) in previewTimes" :key="index" :timestamp="formatTime(time)">
          第 {{ index + 1 }} 次
        </el-timeline-item>
      </el-timeline>
      <el-empty v-if="!previewTimes.length" description="在未来 200 次探测内找不到可执行时刻，请检查执行窗口" />
    </el-dialog>
    <el-drawer v-model="historyVisible" :title="`任务历史：${current?.name || ''}`" size="640px">
      <el-table v-loading="historyLoading" :data="history" size="small">
        <el-table-column prop="id" label="#" width="70" />
        <el-table-column label="发起" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.trigger === 'scheduled' ? 'primary' : 'info'">
              {{ row.trigger === 'scheduled' ? '定时' : '手工' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="结果" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="lastStatusType(row.status)">
              {{ lastStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="成功/无变更/失败" min-width="130">
          <template #default="{ row }">
            {{ row.succeeded }} / {{ row.unchanged }} / {{ row.failed }}
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="160">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="100">
          <template #default="{ row }">{{ formatDuration(durationOf(row)) }}</template>
        </el-table-column>
      </el-table>
      <el-pagination
        v-if="historyTotal > historyPageSize"
        layout="prev, pager, next"
        :total="historyTotal"
        :page-size="historyPageSize"
        :current-page="historyPage"
        style="margin-top: 12px"
        @current-change="loadHistory"
      />
      <el-empty v-if="!historyLoading && !history.length" description="该策略还没有发起过任务" />
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createSchedule,
  deleteSchedule,
  listDevices,
  listGroups,
  listSchedules,
  listTasks,
  listVendors,
  previewSchedule,
  runScheduleNow,
  updateSchedule
} from '../api/endpoints'
import { formatDuration, formatTime } from '../utils/format'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const canWrite = computed(() => auth.atLeast('operator'))
const isAdmin = computed(() => auth.canAdmin)

//: 策略上的"上次结果"。比通用任务状态多两个值：
//: skipped_busy = 上一轮还没跑完被跳过；error = 连任务都没发起来（范围解析失败等）
const SCHEDULE_STATUS = {
  queued: { label: '排队中', type: 'info' },
  running: { label: '运行中', type: 'primary' },
  success: { label: '成功', type: 'success' },
  partial: { label: '部分失败', type: 'warning' },
  failed: { label: '失败', type: 'danger' },
  cancelled: { label: '已取消', type: 'info' },
  skipped_busy: { label: '已跳过（上轮未结束）', type: 'info' },
  error: { label: '发起失败', type: 'danger' }
}

const loading = ref(false)
const saving = ref(false)
const schedules = ref([])
const groups = ref([])
const devices = ref([])
const vendors = ref([])

const formVisible = ref(false)
const previewVisible = ref(false)
const previewTimes = ref([])
const previewJitter = ref(false)

// 任务历史（按 schedule_id 过滤任务列表）
const historyVisible = ref(false)
const historyLoading = ref(false)
const history = ref([])
const historyTotal = ref(0)
const historyPage = ref(1)
const historyPageSize = 10
const current = ref(null)
const formRef = ref()
const windowRange = ref(null)

const form = reactive({
  id: null,
  name: '',
  kind: 'cron',
  cron: '0 2 * * *',
  interval_minutes: 1440,
  jitter_minutes: 5,
  enabled: true,
  scopeType: 'all',
  group_id: null,
  site: '',
  vendor: '',
  device_ids: [],
  include_disabled: false,
  precheck: false,
  retries: 1
})

const rules = {
  name: [{ required: true, message: '请输入策略名称', trigger: 'blur' }],
  cron: [{ required: true, message: '请输入 cron 表达式', trigger: 'blur' }]
}

// 「上次结果」用中文标签；服务层可能写入的 skipped_busy / error 也在这里映射
const lastStatusLabel = (status) => SCHEDULE_STATUS[status]?.label || status
const lastStatusType = (status) => SCHEDULE_STATUS[status]?.type || 'info'

function scopeText(row) {
  const scope = row.scope || {}
  if (scope.all_enabled) return '全部启用设备'
  if (scope.device_ids) return `指定 ${scope.device_ids.length} 台设备`
  if (scope.group_id) {
    const group = groups.value.find((item) => item.id === scope.group_id)
    return `分组：${group ? group.name : `#${scope.group_id}`}`
  }
  if (scope.site) return `站点：${scope.site}`
  if (scope.vendor) return `厂商：${scope.vendor}`
  return '未设置'
}

async function load() {
  loading.value = true
  try {
    schedules.value = await listSchedules()
  } finally {
    loading.value = false
  }
}

async function loadRefs() {
  const [groupList, devicePage, vendorList] = await Promise.all([
    listGroups(),
    listDevices({ limit: 500 }),
    listVendors()
  ])
  groups.value = groupList
  devices.value = devicePage.items
  vendors.value = vendorList
}

function resetForm(row) {
  const scope = row?.scope || {}
  Object.assign(form, {
    id: row?.id || null,
    name: row?.name || '',
    kind: row?.kind || 'cron',
    cron: row?.cron || '0 2 * * *',
    interval_minutes: row?.interval_minutes || 1440,
    jitter_minutes: row?.jitter_minutes ?? 5,
    enabled: row?.enabled ?? true,
    scopeType: scope.all_enabled
      ? 'all'
      : scope.device_ids
        ? 'devices'
        : scope.group_id
          ? 'group'
          : scope.site
            ? 'site'
            : scope.vendor
              ? 'vendor'
              : 'all',
    group_id: scope.group_id || null,
    site: scope.site || '',
    vendor: scope.vendor || '',
    device_ids: scope.device_ids || [],
    include_disabled: Boolean(scope.include_disabled),
    precheck: Boolean(row?.params?.precheck),
    retries: row?.params?.retries ?? 1
  })
  windowRange.value = row?.window_start && row?.window_end ? [row.window_start, row.window_end] : null
}

function buildPayload() {
  const payload = {
    name: form.name,
    kind: form.kind,
    cron: form.kind === 'cron' ? form.cron : null,
    interval_minutes: form.kind === 'interval' ? form.interval_minutes : null,
    jitter_minutes: form.jitter_minutes,
    window_start: windowRange.value?.[0] || null,
    window_end: windowRange.value?.[1] || null,
    enabled: form.enabled,
    precheck: form.precheck,
    retries: form.retries,
    include_disabled: form.include_disabled
  }
  // 范围字段互斥：只带选中的那一种，避免"看起来选了分组其实还是全量"
  if (form.scopeType === 'all') payload.all_enabled = true
  if (form.scopeType === 'group') payload.group_id = form.group_id
  if (form.scopeType === 'site') payload.site = form.site
  if (form.scopeType === 'vendor') payload.vendor = form.vendor
  if (form.scopeType === 'devices') payload.device_ids = form.device_ids
  return payload
}

function openCreate() {
  resetForm(null)
  formVisible.value = true
}

function openEdit(row) {
  resetForm(row)
  formVisible.value = true
}

async function submitForm() {
  await formRef.value.validate()
  const payload = buildPayload()
  if (form.scopeType === 'group' && !payload.group_id) {
    ElMessage.warning('请选择分组')
    return
  }
  if (form.scopeType === 'devices' && !payload.device_ids.length) {
    ElMessage.warning('请选择设备')
    return
  }
  saving.value = true
  try {
    if (form.id) {
      await updateSchedule(form.id, payload)
    } else {
      await createSchedule(payload)
    }
    ElMessage.success('已保存')
    formVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function toggle(row, value) {
  try {
    await updateSchedule(row.id, { enabled: value })
    ElMessage.success(value ? '已启用' : '已停用')
  } finally {
    await load()
  }
}

async function runNow(row) {
  await ElMessageBox.confirm(
    `立即按策略「${row.name}」的范围执行一次备份？这不影响它的定时节奏。`,
    '立即执行',
    { type: 'info' }
  )
  const task = await runScheduleNow(row.id)
  ElMessage.success(`已发起任务 #${task.id}（${task.total} 台设备），可在「备份任务」里跟踪`)
  await load()
}

async function openPreview(row) {
  const result = await previewSchedule(row.id, 5)
  previewTimes.value = result.times || []
  previewJitter.value = Boolean(row.jitter_minutes)
  previewVisible.value = true
}

/** 单次任务的墙钟耗时（没结束的任务返回 0，展示成 '-'）。 */
function durationOf(row) {
  if (!row.started_at || !row.finished_at) return null
  return new Date(row.finished_at) - new Date(row.started_at)
}

async function loadHistory(page = 1) {
  historyPage.value = page
  historyLoading.value = true
  try {
    const result = await listTasks({
      schedule_id: current.value.id,
      limit: historyPageSize,
      offset: (page - 1) * historyPageSize
    })
    history.value = result.items
    historyTotal.value = result.total
  } finally {
    historyLoading.value = false
  }
}

async function openHistory(row) {
  current.value = row
  history.value = []
  historyTotal.value = 0
  historyVisible.value = true
  await loadHistory(1)
}

async function remove(row) {
  await ElMessageBox.confirm(`删除策略「${row.name}」？已产生的任务记录会保留。`, '确认删除', {
    type: 'warning'
  })
  await deleteSchedule(row.id)
  ElMessage.success('已删除')
  await load()
}

// 打开表单时若参考数据还没加载，顺手补上（分组/设备/厂商都要用）
watch(formVisible, async (visible) => {
  if (visible && !groups.value.length) {
    await loadRefs()
  }
})

onMounted(() => {
  load()
  loadRefs()
})
</script>

<style scoped>
.sub {
  color: #909399;
  font-size: 12px;
}
</style>
