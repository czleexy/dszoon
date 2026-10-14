<template>
  <div v-loading="loading">
    <el-page-header @back="router.push('/devices')">
      <template #content>
        <span>{{ device?.name || '设备详情' }}</span>
        <el-tag v-if="device?.last_status" :type="runStatusType(device.last_status)" size="small" style="margin-left: 8px">
          {{ runStatusLabel(device.last_status) }}
        </el-tag>
      </template>
      <template #extra>
        <el-button size="small" :disabled="!auth.canOperate" @click="runTest">
          <el-icon><Connection /></el-icon>连接测试
        </el-button>
        <el-button size="small" type="primary" :disabled="!auth.canOperate" @click="runBackup">
          <el-icon><Refresh /></el-icon>立即备份
        </el-button>
        <el-button size="small" @click="load"><el-icon><RefreshRight /></el-icon>刷新</el-button>
      </template>
    </el-page-header>

    <el-card shadow="never" class="page-card" style="margin-top: 12px">
      <el-descriptions :column="4" border size="small">
        <el-descriptions-item label="管理地址">{{ device?.host }}:{{ device?.port }}</el-descriptions-item>
        <el-descriptions-item label="厂商">{{ device?.vendor }}</el-descriptions-item>
        <el-descriptions-item label="设备名">{{ device?.hostname || '-' }}</el-descriptions-item>
        <el-descriptions-item label="站点">{{ device?.site || '-' }}</el-descriptions-item>
        <el-descriptions-item label="型号">{{ device?.model || '-' }}</el-descriptions-item>
        <el-descriptions-item label="软件版本">{{ device?.os_version || '-' }}</el-descriptions-item>
        <el-descriptions-item label="凭据">{{ device?.credential_profile_name || '未绑定' }}</el-descriptions-item>
        <el-descriptions-item label="最近采集">{{ formatTime(device?.last_backup_at) }}</el-descriptions-item>
        <el-descriptions-item label="版本数">{{ device?.snapshot_count ?? 0 }}</el-descriptions-item>
        <el-descriptions-item label="最新 sha">
          <span class="mono">{{ (device?.latest_sha256 || '').slice(0, 12) || '-' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="标签" :span="2">
          <el-tag v-for="tag in device?.tags || []" :key="tag" size="small" style="margin-right: 4px">
            {{ tag }}
          </el-tag>
          <span v-if="!device?.tags?.length">-</span>
        </el-descriptions-item>
        <el-descriptions-item v-if="device?.last_error" label="最近错误" :span="4">
          <el-text type="danger">{{ device.last_error }}</el-text>
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card shadow="never" class="page-card">
      <template #header>
        <div class="toolbar" style="margin: 0">
          <span>历史版本</span>
          <div class="spacer" />
          <el-text type="info" size="small">
            在表格中勾选两个版本即可对比（按时间先后自动识别）
          </el-text>
          <el-button size="small" :disabled="selectedSnapshots.length !== 2" @click="compare">
            对比选中版本
          </el-button>
        </div>
      </template>
      <el-table
        :data="snapshots"
        size="small"
        empty-text="还没有版本，点「立即备份」采集一次"
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="46" />
        <el-table-column prop="id" label="#" width="70" />
        <el-table-column label="采集时间" width="170">
          <template #default="{ row }">{{ formatTime(row.captured_at) }}</template>
        </el-table-column>
        <el-table-column label="sha" width="130">
          <template #default="{ row }"><span class="mono">{{ row.sha256.slice(0, 12) }}</span></template>
        </el-table-column>
        <el-table-column prop="line_count" label="行数" width="80" />
        <el-table-column label="大小" width="100">
          <template #default="{ row }">{{ formatBytes(row.size_bytes) }}</template>
        </el-table-column>
        <el-table-column prop="model" label="型号" width="150" show-overflow-tooltip />
        <el-table-column prop="os_version" label="软件版本" width="120" />
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="viewSnapshot(row)">查看</el-button>
            <el-button
              link
              size="small"
              :disabled="snapshots.length < 2 || row.id === oldestSnapshotId"
              @click="diffWithPrevious(row)"
            >
              与上一版对比
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card v-if="activeSnapshotId" shadow="never">
      <template #header>
        <div class="toolbar" style="margin: 0">
          <span>配置内容（版本 #{{ activeSnapshotId }}）</span>
          <div class="spacer" />
          <el-button link size="small" @click="activeSnapshotId = null">收起</el-button>
        </div>
      </template>
      <ConfigViewer :snapshot-id="activeSnapshotId" :device-name="device?.name || 'device'" />
    </el-card>

    <el-card v-if="diff" shadow="never" style="margin-top: 16px">
      <template #header>
        <div class="toolbar" style="margin: 0">
          <span>
            版本对比：{{ diff.from_snapshot_id }} → {{ diff.to_snapshot_id }}
          </span>
          <div class="spacer" />
          <el-button link size="small" @click="diff = null">收起</el-button>
        </div>
      </template>
      <DiffViewer :diff="diff" />
    </el-card>

    <el-card v-if="runs.length" shadow="never" style="margin-top: 16px">
      <template #header>采集记录</template>
      <el-table :data="runs" size="small">
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ formatTime(row.started_at) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="runStatusType(row.status)" size="small">{{ runStatusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="100">
          <template #default="{ row }">{{ formatDuration(row.duration_ms) }}</template>
        </el-table-column>
        <el-table-column label="变更" width="90">
          <template #default="{ row }">
            <span v-if="row.changed === null">首次</span>
            <el-tag v-else :type="row.changed ? 'warning' : 'info'" size="small">
              {{ row.changed ? '有变更' : '无变更' }}
            </el-tag>
          </template>
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
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import ConfigViewer from '../components/ConfigViewer.vue'
import DiffViewer from '../components/DiffViewer.vue'
import {
  backupDevice,
  getDevice,
  getDiff,
  listRuns,
  listSnapshots,
  testDevice
} from '../api/endpoints'
import { useAuthStore } from '../stores/auth'
import {
  formatBytes,
  formatDuration,
  formatTime,
  runStatusLabel,
  runStatusType
} from '../utils/format'

const props = defineProps({ id: { type: [String, Number], required: true } })
const auth = useAuthStore()
const router = useRouter()

const loading = ref(false)
const device = ref(null)
const snapshots = ref([])
const runs = ref([])
const selectedSnapshots = ref([])
const activeSnapshotId = ref(null)
const diff = ref(null)

//: 表格按时间倒序，"上一版"在数组里是后一项；最早的版本没有上一版。
//  （这里曾经把判断写反成"最新版不可对比"——恰恰是最常用的操作被禁用了）
const oldestSnapshotId = computed(() =>
  snapshots.value.length ? snapshots.value[snapshots.value.length - 1].id : null
)

async function load() {
  loading.value = true
  try {
    const deviceId = Number(props.id)
    const [detail, snapshotPage, runPage] = await Promise.all([
      getDevice(deviceId),
      listSnapshots(deviceId, { limit: 100 }),
      listRuns({ device_id: deviceId, limit: 20 })
    ])
    device.value = detail
    snapshots.value = snapshotPage.items
    runs.value = runPage.items
  } finally {
    loading.value = false
  }
}

function viewSnapshot(row) {
  activeSnapshotId.value = row.id
  diff.value = null
}

// 见 DevicesView 的同类注释：不用模板内联赋值，避免依赖 ref 自动解包
function onSelectionChange(rows) {
  selectedSnapshots.value = rows
}

async function compare() {
  const [first, second] = selectedSnapshots.value
  if (!first || !second) return
  // 表格按时间倒序，id 大的更新
  const older = first.id < second.id ? first : second
  const newer = first.id < second.id ? second : first
  diff.value = await getDiff(Number(props.id), {
    from_snapshot_id: older.id,
    to_snapshot_id: newer.id
  })
  activeSnapshotId.value = null
}

async function diffWithPrevious(row) {
  const index = snapshots.value.findIndex((item) => item.id === row.id)
  const previous = snapshots.value[index + 1]
  if (!previous) {
    ElMessage.info('这是最早的版本，没有可对比的上一版')
    return
  }
  diff.value = await getDiff(Number(props.id), {
    from_snapshot_id: previous.id,
    to_snapshot_id: row.id
  })
  activeSnapshotId.value = null
}

async function runBackup() {
  const task = await backupDevice(Number(props.id))
  ElMessage.success(`任务 #${task.id} 已创建，可在「备份任务」里看进度`)
  router.push('/tasks')
}

async function runTest() {
  const result = await testDevice(Number(props.id))
  if (result.ok) {
    ElMessage.success(`连接成功：${result.facts?.model || result.hostname || ''}`)
    await load()
  } else {
    ElMessage.error(result.error_message || '连接失败')
  }
}

onMounted(load)
</script>

<style scoped>
.muted {
  color: #909399;
  font-size: 12px;
}
</style>
