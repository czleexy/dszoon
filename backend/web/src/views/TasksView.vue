<template>
  <div>
    <el-card shadow="never" class="page-card">
      <div class="toolbar">
        <el-button @click="load"><el-icon><RefreshRight /></el-icon>刷新</el-button>
        <el-checkbox v-model="autoRefresh">自动刷新（3s）</el-checkbox>
        <div class="spacer" />
        <el-text type="info" size="small">点击任务行查看每台设备的结果与实时日志</el-text>
      </div>

      <el-table v-loading="loading" :data="tasks" empty-text="还没有备份任务" @row-click="openDetail">
        <el-table-column prop="id" label="#" width="70" />
        <el-table-column prop="name" label="任务" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.name || `任务 #${row.id}` }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="taskStatusType(row.status)" size="small">{{ taskStatusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" width="200">
          <template #default="{ row }">
            <el-progress
              :percentage="progressOf(row)"
              :status="row.status === 'failed' ? 'exception' : row.status === 'success' ? 'success' : undefined"
              :stroke-width="12"
            />
          </template>
        </el-table-column>
        <el-table-column label="结果" min-width="200">
          <template #default="{ row }">
            <el-tag type="success" size="small">成功 {{ row.succeeded }}</el-tag>
            <!-- 「无变更」是成功里的子集（进度条按 成功+失败 计算），所以写"其中"避免误读成两类之和 -->
            <el-tag type="info" size="small" style="margin-left: 4px">其中无变更 {{ row.unchanged }}</el-tag>
            <el-tag v-if="row.failed" type="danger" size="small" style="margin-left: 4px">失败 {{ row.failed }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_by" label="发起人" width="100" />
        <el-table-column label="发起" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.trigger === 'scheduled' ? 'primary' : 'info'">
              {{ row.trigger === 'scheduled' ? '定时' : '手工' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button
              link
              type="danger"
              size="small"
              :disabled="!auth.canOperate || !['queued', 'running'].includes(row.status)"
              @click.stop="cancel(row)"
            >
              取消
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-drawer
      v-model="drawerVisible"
      :title="`任务 #${detail?.id ?? ''}`"
      size="70%"
      @close="onDrawerClose"
    >
      <template v-if="detail">
        <el-descriptions :column="3" border size="small">
          <el-descriptions-item label="状态">
            <el-tag :type="taskStatusType(detail.status)" size="small">
              {{ taskStatusLabel(detail.status) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="发起人">{{ detail.created_by || '-' }}</el-descriptions-item>
          <!-- 「定时还是手工」是最常被问的一句：突然冒出来的备份要先能确认是谁触发的 -->
          <el-descriptions-item label="发起方式">
            {{ detail.trigger === 'scheduled' ? '定时（策略自动发起）' : '手工' }}
          </el-descriptions-item>
          <el-descriptions-item label="开始">{{ formatTime(detail.started_at) }}</el-descriptions-item>
          <el-descriptions-item label="结束">{{ formatTime(detail.finished_at) }}</el-descriptions-item>
          <el-descriptions-item label="设备数">{{ detail.total }}</el-descriptions-item>
          <el-descriptions-item label="耗时">
            {{ detail.finished_at && detail.started_at ? formatDuration((new Date(detail.finished_at) - new Date(detail.started_at))) : '-' }}
          </el-descriptions-item>
        </el-descriptions>

        <el-progress
          style="margin: 16px 0"
          :percentage="liveProgress"
          :status="detail.status === 'failed' ? 'exception' : detail.status === 'success' ? 'success' : undefined"
        />
        <div class="toolbar">
          <el-tag type="success" size="small">成功 {{ counts.succeeded }}</el-tag>
          <el-tag type="info" size="small">其中无变更 {{ counts.unchanged }}</el-tag>
          <el-tag type="danger" size="small">失败 {{ counts.failed }}</el-tag>
          <el-tag size="small">已处理 {{ counts.done }} / {{ counts.total }}</el-tag>
          <div class="spacer" />
          <el-tag v-if="streaming" type="primary" size="small">
            <el-icon class="is-loading"><Loading /></el-icon> 实时接收中
          </el-tag>
        </div>

        <el-card shadow="never" style="margin: 12px 0">
          <template #header>每台设备结果</template>
          <el-table :data="detail.runs" size="small" empty-text="暂无结果">
            <el-table-column prop="device_name" label="设备" min-width="140" />
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag :type="runStatusType(row.status)" size="small">{{ runStatusLabel(row.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="变更" width="90">
              <template #default="{ row }">
                <span v-if="row.changed === null">首次</span>
                <el-tag v-else :type="row.changed ? 'warning' : 'info'" size="small">
                  {{ row.changed ? '有变更' : '无变更' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="耗时" width="100">
              <template #default="{ row }">{{ formatDuration(row.duration_ms) }}</template>
            </el-table-column>
            <el-table-column prop="line_count" label="行数" width="80" />
            <el-table-column label="错误/警告" min-width="240">
              <template #default="{ row }">
                <el-text v-if="row.error_message" type="danger">{{ row.error_message }}</el-text>
                <div v-for="(w, i) in row.warnings || []" :key="i" class="muted">{{ w }}</div>
                <span v-if="!row.error_message && !row.warnings?.length">-</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card v-if="logLines.length" shadow="never">
          <template #header>实时日志（最近 {{ logLines.length }} 条）</template>
          <pre class="log-body mono">{{ logLines.join('\n') }}</pre>
        </el-card>
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { streamTask } from '../api/sse'
import { cancelTask, getTask, listTasks } from '../api/endpoints'
import { useAuthStore } from '../stores/auth'
import {
  formatDuration,
  formatTime,
  runStatusLabel,
  runStatusType,
  taskStatusLabel,
  taskStatusType
} from '../utils/format'

const auth = useAuthStore()
const loading = ref(false)
const tasks = ref([])
const autoRefresh = ref(true)
const drawerVisible = ref(false)
const detail = ref(null)
const streaming = ref(false)
const logLines = ref([])
const counts = reactive({ done: 0, total: 0, succeeded: 0, failed: 0, unchanged: 0 })

let pollTimer = null
let stopStream = null

const liveProgress = computed(() => {
  if (!counts.total) return detail.value?.total ? 0 : 0
  return Math.min(100, Math.round(((counts.succeeded + counts.failed) * 100) / counts.total))
})

function progressOf(task) {
  if (!task.total) return 0
  return Math.min(100, Math.round(((task.succeeded + task.failed) * 100) / task.total))
}

async function load() {
  loading.value = true
  try {
    const page = await listTasks({ limit: 50 })
    tasks.value = page.items
    if (detail.value) {
      const current = page.items.find((item) => item.id === detail.value.id)
      if (current) detail.value = { ...detail.value, ...current }
    }
  } finally {
    loading.value = false
    schedulePoll()
  }
}

function schedulePoll() {
  clearTimeout(pollTimer)
  if (!autoRefresh.value) return
  const active = tasks.value.some((task) => ['queued', 'running'].includes(task.status))
  if (active) {
    pollTimer = setTimeout(load, 3000)
  }
}

async function openDetail(row) {
  counts.total = row.total
  counts.done = row.succeeded + row.failed
  counts.succeeded = row.succeeded
  counts.failed = row.failed
  counts.unchanged = row.unchanged
  logLines.value = []
  detail.value = await getTask(row.id)
  drawerVisible.value = true
  if (['queued', 'running'].includes(row.status)) {
    startStream(row.id)
  }
}

function onDrawerClose() {
  // 关掉抽屉就断开 SSE：否则用户关掉界面后连接还在，长任务会一直白占着。
  // 列表本身仍有 3 秒轮询兜底，不会丢进度。
  stopStream?.()
  stopStream = null
  streaming.value = false
}

async function refreshDetail() {
  if (!detail.value) return
  detail.value = await getTask(detail.value.id)
  await load()
}

function startStream(taskId) {
  stopStream?.()
  streaming.value = true
  stopStream = streamTask(taskId, {
    onEvent(event) {
      const data = event.data || {}
      if (event.type === 'device_finished') {
        counts.done = data.done ?? counts.done
        counts.total = data.total ?? counts.total
        counts.succeeded = data.succeeded ?? counts.succeeded
        counts.failed = data.failed ?? counts.failed
        counts.unchanged = data.unchanged ?? counts.unchanged
        const mark = data.error_code ? '✗' : data.changed === false ? '=' : data.changed ? '±' : '+'
        logLines.value.unshift(
          `${mark} ${data.device_name || data.device_id}  ${data.run_status}` +
            (data.error_message ? `  ${data.error_message}` : '') +
            (data.changed === false ? '  （内容无变化）' : '')
        )
        logLines.value = logLines.value.slice(0, 200)
      } else if (event.type === 'task_started') {
        logLines.value.unshift(`▶ 任务开始，共 ${data.total} 台设备`)
      } else if (event.type === 'task_finished') {
        logLines.value.unshift(`■ 任务结束：${data.status}`)
        streaming.value = false
        refreshDetail()
      }
    },
    onError() {
      // SSE 失败就降级为轮询，不让用户干等
      streaming.value = false
      autoRefresh.value = true
      schedulePoll()
    },
    onDone() {
      streaming.value = false
    }
  })
}

async function cancel(row) {
  const result = await cancelTask(row.id)
  ElMessage[result.ok ? 'success' : 'info'](result.message)
  await load()
}

onMounted(load)
onUnmounted(() => {
  clearTimeout(pollTimer)
  stopStream?.()
})
</script>

<style scoped>
.muted {
  color: #909399;
  font-size: 12px;
}
.log-body {
  margin: 0;
  max-height: 260px;
  overflow: auto;
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 10px;
  border-radius: 4px;
  font-size: 12px;
  line-height: 1.6;
}
</style>
