<template>
  <div v-loading="loading">
    <div class="toolbar">
      <el-radio-group v-model="format" size="small" @change="load">
        <el-radio-button value="redacted">脱敏</el-radio-button>
        <el-radio-button
          value="raw"
          :disabled="!auth.canSeePlaintext"
        >
          明文
        </el-radio-button>
      </el-radio-group>
      <el-tag v-if="format === 'raw'" type="danger" size="small">
        明文查看会被记入审计日志
      </el-tag>
      <div class="spacer" />
      <el-text type="info" size="small">
        {{ meta.line_count }} 行 · {{ formatBytes(meta.size_bytes) }} · sha {{ (meta.sha256 || '').slice(0, 12) }}
      </el-text>
      <el-button size="small" @click="download"><el-icon><Download /></el-icon>下载</el-button>
    </div>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="error"
    />
    <pre v-else class="config-body mono">{{ content || '（空）' }}</pre>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { downloadSnapshot, getSnapshotContent } from '../api/endpoints'
import { useAuthStore } from '../stores/auth'
import { formatBytes } from '../utils/format'

const props = defineProps({
  snapshotId: { type: Number, required: true },
  deviceName: { type: String, default: 'device' }
})

const auth = useAuthStore()
const loading = ref(false)
const format = ref('redacted')
const content = ref('')
const error = ref('')
const meta = reactive({ size_bytes: 0, line_count: 0, sha256: '' })

async function load() {
  if (!props.snapshotId) return
  loading.value = true
  error.value = ''
  try {
    const data = await getSnapshotContent(props.snapshotId, { format: format.value })
    content.value = data.content
    meta.size_bytes = data.size_bytes
    meta.line_count = data.line_count
    meta.sha256 = data.sha256
  } catch (err) {
    if (err.response?.status === 403) {
      error.value = '查看配置明文需要「审计」及以上角色'
      format.value = 'redacted'
    } else {
      error.value = err.response?.data?.detail || '加载失败'
    }
    content.value = ''
  } finally {
    loading.value = false
  }
}

async function download() {
  const name = `${props.deviceName}-${props.snapshotId}${format.value === 'raw' ? '' : '.redacted'}.cfg`
  await downloadSnapshot(props.snapshotId, format.value, name)
  ElMessage.success('已开始下载（已记入审计）')
}

watch(() => props.snapshotId, load)
onMounted(load)
</script>

<style scoped>
.config-body {
  margin: 0;
  max-height: 620px;
  overflow: auto;
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 12px;
  border-radius: 4px;
  line-height: 1.5;
  white-space: pre;
}
</style>
