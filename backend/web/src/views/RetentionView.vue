<template>
  <div>
    <el-card shadow="never" class="page-card" v-loading="loading">
      <template #header>
        <div class="card-header">
          <span>保留策略</span>
          <el-text type="info" size="small">
            默认很保守：最早与最新版本、以及发生变更的版本都会长期保留
          </el-text>
        </div>
      </template>

      <el-alert
        v-if="!isAdmin"
        type="info"
        :closable="false"
        show-icon
        title="只有管理员可以修改保留策略或手工执行清理"
        style="margin-bottom: 12px"
      />

      <el-form :model="form" label-width="150px" style="max-width: 720px">
        <el-form-item label="启用自动清理">
          <el-switch v-model="form.enabled" :disabled="!isAdmin" />
          <el-text type="info" size="small" style="margin-left: 8px">
            停用后连"扫一遍"都不做
          </el-text>
        </el-form-item>

        <el-form-item label="变更版本保留">
          <el-switch
            v-model="keepChangedForever"
            :disabled="!isAdmin"
            active-text="永久保留"
            inactive-text="按天数"
          />
          <el-input-number
            v-if="!keepChangedForever"
            v-model="form.keep_changed_days"
            :min="1"
            :max="3650"
            :disabled="!isAdmin"
            style="margin-left: 12px"
          />
          <el-text v-if="!keepChangedForever" type="info" size="small" style="margin-left: 8px">天</el-text>
        </el-form-item>

        <el-form-item label="未变更版本按天折叠">
          <el-input-number
            v-model="form.keep_unchanged_daily_days"
            :min="0"
            :max="3650"
            :disabled="!isAdmin"
          />
          <el-text type="info" size="small" style="margin-left: 8px">
            天内每天只留 1 个版本（内容没变的重复采集不必逐条留档）
          </el-text>
        </el-form-item>

        <el-form-item label="每台设备最少保留">
          <el-input-number v-model="form.keep_min_versions" :min="1" :max="1000" :disabled="!isAdmin" />
          <el-text type="info" size="small" style="margin-left: 8px">个版本（含最早 1 个）</el-text>
        </el-form-item>

        <el-form-item label="会话日志保留">
          <el-input-number
            v-model="form.keep_session_log_days"
            :min="0"
            :max="3650"
            :disabled="!isAdmin"
          />
          <el-text type="info" size="small" style="margin-left: 8px">天（0 = 不清理）</el-text>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="saving" :disabled="!isAdmin" @click="save">保存</el-button>
          <el-button
            type="danger"
            plain
            :loading="running"
            :disabled="!isAdmin"
            @click="runNow"
          >
            立即清理一次
          </el-button>
        </el-form-item>
      </el-form>

      <el-descriptions :column="1" border style="max-width: 720px">
        <el-descriptions-item label="当前规则">{{ policy.description || '-' }}</el-descriptions-item>
        <el-descriptions-item label="上次清理">
          {{ formatTime(policy.last_run_at) }}
          <template v-if="policy.last_run_at">
            ：删除 {{ policy.last_result?.deleted_snapshots || 0 }} 个版本、
            回收 {{ policy.last_result?.deleted_blobs || 0 }} 个正文文件、
            释放 {{ formatBytes(policy.last_result?.freed_bytes || 0) }}、
            清理 {{ policy.last_result?.deleted_session_logs || 0 }} 个会话日志
          </template>
        </el-descriptions-item>
      </el-descriptions>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getRetention, runRetention, updateRetention } from '../api/endpoints'
import { formatBytes, formatTime } from '../utils/format'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
// 路由守卫在进入页面前已经 await 过 auth.loadMe()，所以这里能直接拿到角色
const isAdmin = computed(() => auth.canAdmin)

const loading = ref(false)
const saving = ref(false)
const running = ref(false)
const policy = ref({})
const keepChangedForever = ref(true)
const form = reactive({
  enabled: true,
  keep_changed_days: 90,
  keep_unchanged_daily_days: 90,
  keep_min_versions: 2,
  keep_session_log_days: 7
})

onMounted(load)

// 「永久保留」开关关掉时给个合理默认值，避免出现空输入框
watch(keepChangedForever, (forever) => {
  if (!forever && !form.keep_changed_days) {
    form.keep_changed_days = 90
  }
})

async function load() {
  loading.value = true
  try {
    policy.value = await getRetention()
    Object.assign(form, {
      enabled: policy.value.enabled,
      keep_changed_days: policy.value.keep_changed_days || 90,
      keep_unchanged_daily_days: policy.value.keep_unchanged_daily_days,
      keep_min_versions: policy.value.keep_min_versions,
      keep_session_log_days: policy.value.keep_session_log_days
    })
    keepChangedForever.value = policy.value.keep_changed_days === null
  } finally {
    loading.value = false
  }
}

function buildPayload() {
  return {
    enabled: form.enabled,
    // 传 null = 改回"永久保留"（后端把显式 null 当清空处理）
    keep_changed_days: keepChangedForever.value ? null : form.keep_changed_days,
    keep_unchanged_daily_days: form.keep_unchanged_daily_days,
    keep_min_versions: form.keep_min_versions,
    keep_session_log_days: form.keep_session_log_days
  }
}

async function save() {
  saving.value = true
  try {
    policy.value = await updateRetention(buildPayload())
    ElMessage.success('保留策略已更新')
  } finally {
    saving.value = false
  }
}

async function runNow() {
  await ElMessageBox.confirm(
    '立即按当前策略执行一次清理？删除的版本与其配置正文无法恢复。',
    '确认执行清理',
    { type: 'warning', confirmButtonText: '执行清理' }
  )
  running.value = true
  try {
    const result = await runRetention()
    const summary = result.summary || {}
    ElMessage.success(
      `已清理：删除 ${summary.deleted_snapshots || 0} 个版本、回收 ${summary.deleted_blobs || 0} 个文件`
    )
    await load()
  } finally {
    running.value = false
  }
}
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  font-weight: 600;
}
</style>
