<template>
  <div v-loading="loading">
    <el-row :gutter="16">
      <el-col v-for="card in cards" :key="card.label" :span="6">
        <el-card class="page-card stat" shadow="never">
          <div class="stat-value" :style="{ color: card.color }">{{ card.value }}</div>
          <div class="stat-label">{{ card.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="page-card">
      <template #header>
        <div class="toolbar" style="margin: 0">
          <span>最近备份任务</span>
          <div class="spacer" />
          <el-button size="small" @click="goBackupAll" :disabled="!auth.canOperate">
            <el-icon><Refresh /></el-icon>备份全部启用设备
          </el-button>
        </div>
      </template>
      <el-table :data="tasks" size="small" empty-text="还没有备份任务">
        <el-table-column prop="id" label="#" width="70" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="taskStatusType(row.status)" size="small">
              {{ taskStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" width="180">
          <template #default="{ row }">
            <el-progress
              :percentage="progressOf(row)"
              :status="row.status === 'failed' ? 'exception' : undefined"
              :stroke-width="12"
            />
          </template>
        </el-table-column>
        <el-table-column label="结果" min-width="200">
          <template #default="{ row }">
            成功 {{ row.succeeded }}（其中无变更 {{ row.unchanged }}）/ 失败 {{ row.failed }}
            （共 {{ row.total }}）
          </template>
        </el-table-column>
        <el-table-column label="发起人" prop="created_by" width="100" />
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="router.push('/tasks')">
              查看
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card v-if="Object.keys(stats.run_status_counts || {}).length" shadow="never">
      <template #header>历史采集状态分布</template>
      <el-space wrap>
        <el-tag
          v-for="(count, status) in stats.run_status_counts"
          :key="status"
          :type="runStatusType(status)"
          size="large"
        >
          {{ runStatusLabel(status) }}：{{ count }}
        </el-tag>
      </el-space>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { createBackup, fetchStats, listTasks } from '../api/endpoints'
import { useAuthStore } from '../stores/auth'
import { formatTime, runStatusLabel, runStatusType, taskStatusLabel, taskStatusType } from '../utils/format'

const auth = useAuthStore()
const router = useRouter()
const loading = ref(false)
const stats = ref({ devices: { total: 0, enabled: 0 }, snapshots: 0, tasks: 0, failed_last_24h: 0 })
const tasks = ref([])

const cards = computed(() => [
  { label: '设备总数', value: stats.value.devices?.total ?? 0, color: '#409eff' },
  { label: '启用设备', value: stats.value.devices?.enabled ?? 0, color: '#67c23a' },
  { label: '配置版本数', value: stats.value.snapshots ?? 0, color: '#909399' },
  { label: '近 24h 失败采集', value: stats.value.failed_last_24h ?? 0, color: stats.value.failed_last_24h ? '#f56c6c' : '#67c23a' }
])

function progressOf(task) {
  if (!task.total) return 0
  return Math.min(100, Math.round(((task.succeeded + task.failed) * 100) / task.total))
}

async function load() {
  loading.value = true
  try {
    const [statsData, taskPage] = await Promise.all([fetchStats(), listTasks({ limit: 5 })])
    stats.value = statsData
    tasks.value = taskPage.items
  } finally {
    loading.value = false
  }
}

async function goBackupAll() {
  try {
    await ElMessageBox.confirm(
      `将对全部启用设备（${stats.value.devices?.enabled ?? 0} 台）发起配置采集，是否继续？`,
      '确认备份',
      { type: 'warning' }
    )
  } catch {
    return
  }
  const task = await createBackup({ all_enabled: true, name: '看板一键备份' })
  ElMessage.success(`任务 #${task.id} 已创建，共 ${task.total} 台设备`)
  router.push('/tasks')
}

onMounted(load)
</script>

<style scoped>
.stat {
  text-align: center;
}
.stat-value {
  font-size: 28px;
  font-weight: 700;
}
.stat-label {
  color: #909399;
  font-size: 13px;
  margin-top: 4px;
}
</style>
