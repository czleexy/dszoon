<template>
  <div>
    <el-card shadow="never" class="page-card">
      <div class="toolbar">
        <el-button type="primary" :disabled="!canWrite" @click="openCreate">
          <el-icon><Plus /></el-icon>新增渠道
        </el-button>
        <el-button @click="openLogs">
          <el-icon><Tickets /></el-icon>发送记录
        </el-button>
        <div class="spacer" />
        <el-text type="info" size="small">
          通知失败不影响备份本身；机器人地址里的密钥在界面上是打码显示的
        </el-text>
      </div>

      <el-table v-loading="loading" :data="channels">
        <el-table-column prop="name" label="名称" min-width="140" />
        <el-table-column label="类型" width="130">
          <template #default="{ row }">{{ kindLabel(row.kind) }}</template>
        </el-table-column>
        <el-table-column label="订阅事件" min-width="220">
          <template #default="{ row }">
            <el-tag v-for="event in row.events" :key="event" size="small" style="margin: 0 4px 4px 0">
              {{ eventLabel(event) }}
            </el-tag>
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
        <el-table-column label="最近发送" width="200">
          <template #default="{ row }">
            <el-tag v-if="row.last_status" size="small" :type="row.last_status === 'success' ? 'success' : 'danger'">
              {{ row.last_status === 'success' ? '成功' : '失败' }}
            </el-tag>
            <span v-else>-</span>
            <div v-if="row.last_sent_at" class="sub">{{ formatTime(row.last_sent_at) }}</div>
            <el-tooltip v-if="row.last_error" :content="row.last_error" placement="top">
              <div class="sub error">{{ row.last_error }}</div>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="180">
          <template #default="{ row }">
            <el-button link type="primary" size="small" :loading="testing === row.id" @click="sendTest(row)">
              测试
            </el-button>
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

    <el-dialog v-model="formVisible" :title="form.id ? '编辑渠道' : '新增渠道'" width="620px">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="110px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" maxlength="128" />
        </el-form-item>
        <el-form-item label="类型" prop="kind">
          <el-select v-model="form.kind" style="width: 260px">
            <el-option v-for="item in kinds" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>

        <template v-if="form.kind === 'smtp'">
          <el-form-item label="SMTP 服务器" prop="host">
            <el-input v-model="form.host" placeholder="smtp.example.com" />
          </el-form-item>
          <el-form-item label="加密方式">
            <el-radio-group v-model="form.tlsMode">
              <el-radio-button value="ssl">SSL（465）</el-radio-button>
              <el-radio-button value="starttls">STARTTLS（587/25）</el-radio-button>
              <el-radio-button value="none">明文（内网）</el-radio-button>
            </el-radio-group>
            <el-text type="info" size="small">
              后端默认按 SSL 连接；587/25 端口的企业邮箱必须选 STARTTLS，否则会报 SSL 握手错误
            </el-text>
          </el-form-item>
          <el-form-item label="端口">
            <el-input-number v-model="form.port" :min="1" :max="65535" />
          </el-form-item>
          <el-form-item label="发件账号">
            <el-input v-model="form.username" placeholder="noreply@example.com" />
          </el-form-item>
          <el-form-item label="口令">
            <el-input
              v-model="form.password"
              type="password"
              show-password
              :placeholder="form.has_secret ? '留空 = 不修改已保存的口令' : 'SMTP 授权码'"
            />
          </el-form-item>
          <el-form-item label="发件人">
            <el-input v-model="form.from_addr" placeholder="noreply@example.com" />
          </el-form-item>
          <el-form-item label="收件人" prop="to_addrs">
            <el-select
              v-model="form.to_addrs"
              multiple
              filterable
              allow-create
              default-first-option
              placeholder="回车分隔多个邮箱"
              style="width: 100%"
            />
          </el-form-item>
        </template>

        <template v-else>
          <el-form-item label="Webhook 地址" prop="url">
            <el-input v-model="form.url" placeholder="https://..." />
            <el-text type="info" size="small">
              钉钉/企微/飞书的机器人地址可整条粘贴（含 access_token）；保存后界面上会打码
            </el-text>
          </el-form-item>
          <el-form-item v-if="form.kind === 'webhook'" label="校验令牌">
            <el-input
              v-model="form.token"
              :placeholder="form.has_secret ? '留空 = 不修改' : '可选：会随消息体一起发出'"
            />
          </el-form-item>
          <el-form-item v-if="form.kind === 'dingtalk'" label="加签密钥">
            <el-input
              v-model="form.secret"
              :placeholder="form.has_secret ? '留空 = 不修改' : '可选：SEC 开头的加签密钥'"
            />
          </el-form-item>
        </template>

        <el-divider content-position="left">触发规则</el-divider>
        <el-form-item label="订阅事件" prop="events">
          <el-checkbox-group v-model="form.events">
            <el-checkbox v-for="item in events" :key="item.value" :value="item.value">
              {{ item.label }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="失败台数阈值">
          <el-input-number v-model="form.min_failures" :min="1" :max="1000" />
          <el-text type="info" size="small" style="margin-left: 8px">
            失败设备少于该数量时不打扰（1 = 只要有失败就通知）
          </el-text>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitForm">保存</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="logsVisible" title="通知发送记录" size="720px">
      <div class="toolbar">
        <el-select v-model="logFilter" style="width: 140px" @change="loadLogs">
          <el-option label="全部" value="" />
          <el-option label="仅成功" value="success" />
          <el-option label="仅失败" value="failed" />
        </el-select>
        <el-button @click="loadLogs">刷新</el-button>
      </div>
      <el-table v-loading="logLoading" :data="logs" size="small">
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="channel_name" label="渠道" width="120" />
        <el-table-column label="事件" width="120">
          <template #default="{ row }">{{ row.event === 'test' ? '手工测试' : eventLabel(row.event) }}</template>
        </el-table-column>
        <el-table-column label="结果" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'success' ? 'success' : 'danger'">
              {{ row.status === 'success' ? '成功' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="subject" label="主题" min-width="180" show-overflow-tooltip />
        <el-table-column prop="error" label="错误" min-width="160" show-overflow-tooltip />
      </el-table>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createChannel,
  deleteChannel,
  fetchNotificationEvents,
  listChannels,
  listNotificationLogs,
  testChannel,
  updateChannel
} from '../api/endpoints'
import { formatTime } from '../utils/format'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const canWrite = computed(() => auth.atLeast('operator'))
const isAdmin = computed(() => auth.canAdmin)

const loading = ref(false)
const saving = ref(false)
const testing = ref(null)
const channels = ref([])
const kinds = ref([])
const events = ref([])

const logsVisible = ref(false)
const logLoading = ref(false)
const logs = ref([])
const logFilter = ref('')

const formVisible = ref(false)
const formRef = ref()
const form = reactive({
  id: null,
  name: '',
  kind: 'dingtalk',
  enabled: true,
  events: [],
  min_failures: 1,
  has_secret: false,
  // 各类型的配置字段（按 kind 取用）
  url: '',
  token: '',
  secret: '',
  host: '',
  port: 465,
  tlsMode: 'ssl',
  username: '',
  password: '',
  from_addr: '',
  to_addrs: []
})

const rules = {
  name: [{ required: true, message: '请输入渠道名称', trigger: 'blur' }],
  url: [
    {
      validator: (_rule, value, callback) => {
        if (form.kind !== 'smtp' && !value) return callback(new Error('请填写 Webhook 地址'))
        return callback()
      },
      trigger: 'blur'
    }
  ],
  host: [
    {
      validator: (_rule, value, callback) => {
        if (form.kind === 'smtp' && !value) return callback(new Error('请填写 SMTP 服务器'))
        return callback()
      },
      trigger: 'blur'
    }
  ],
  events: [
    {
      validator: (_rule, value, callback) =>
        value && value.length ? callback() : callback(new Error('至少订阅一个事件')),
      trigger: 'change'
    }
  ]
}

const kindLabel = (kind) => kinds.value.find((item) => item.value === kind)?.label || kind
const eventLabel = (event) =>
  events.value.find((item) => item.value === event)?.label ||
  String(event || '')
    .split(',')
    .map((one) => events.value.find((item) => item.value === one)?.label || one)
    .join('、')

async function load() {
  loading.value = true
  try {
    channels.value = await listChannels()
  } finally {
    loading.value = false
  }
}

async function loadMeta() {
  const meta = await fetchNotificationEvents()
  events.value = meta.events
  kinds.value = meta.kinds
}

function resetForm(row) {
  const config = row?.config || {}
  Object.assign(form, {
    id: row?.id || null,
    name: row?.name || '',
    kind: row?.kind || 'dingtalk',
    enabled: row?.enabled ?? true,
    events: row?.events ? [...row.events] : [],
    min_failures: row?.min_failures ?? 1,
    has_secret: Boolean(row?.has_secret),
    url: config.url || '',
    token: '',
    secret: '',
    host: config.host || '',
    port: config.port || 465,
    // use_tls 默认 true（SSL）；显式 false 时才看 starttls
    tlsMode: config.use_tls === false ? (config.starttls ? 'starttls' : 'none') : 'ssl',
    username: config.username || '',
    password: '',
    from_addr: config.from_addr || '',
    // 后端两种都收（数组或逗号分隔的字符串，见 _send_mail_sync），但界面必须拿到数组：
    // 直接 spread 一个字符串会把它拆成单个字符，收件人框里就会显示一堆字母
    to_addrs: normalizeAddrs(config.to_addrs)
  })
}

/** 把 `to_addrs` 统一成数组（后端兼容"数组"与"逗号分隔字符串"两种写法）。 */
function normalizeAddrs(value) {
  if (Array.isArray(value)) return value.filter(Boolean).map((item) => String(item).trim())
  if (typeof value === 'string') {
    return value
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
  }
  return []
}

function openCreate() {
  resetForm(null)
  form.events = events.value.slice(0, 3).map((item) => item.value)
  formVisible.value = true
}

function openEdit(row) {
  resetForm(row)
  formVisible.value = true
}

function buildPayload() {
  if (form.kind === 'smtp') {
    return {
      name: form.name,
      kind: form.kind,
      enabled: form.enabled,
      events: form.events,
      min_failures: form.min_failures,
      config: {
        host: form.host,
        port: form.port,
        username: form.username || null,
        from_addr: form.from_addr || null,
        to_addrs: form.to_addrs,
        // 与后端 _send_mail_sync 的判定保持一致：use_tls=true 走 SMTP_SSL，
        // 否则明文连接、starttls=true 时再升级
        use_tls: form.tlsMode === 'ssl',
        starttls: form.tlsMode === 'starttls'
      },
      // 口令留空 = 不改（服务层只覆盖传进来的键）
      secret: form.password ? { password: form.password } : null
    }
  }
  const secret = {}
  if (form.token) secret.token = form.token
  if (form.secret) secret.secret = form.secret
  return {
    name: form.name,
    kind: form.kind,
    enabled: form.enabled,
    events: form.events,
    min_failures: form.min_failures,
    // ★ 编辑时回传的是打码后的 url，服务层会认出 *** 并保留库里的真实地址
    config: { url: form.url },
    secret: Object.keys(secret).length ? secret : null
  }
}

async function submitForm() {
  await formRef.value.validate()
  if (form.kind === 'smtp' && !form.to_addrs.length) {
    ElMessage.warning('请至少填写一个收件人')
    return
  }
  saving.value = true
  try {
    const payload = buildPayload()
    if (form.id) {
      await updateChannel(form.id, payload)
    } else {
      await createChannel(payload)
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
    await updateChannel(row.id, { enabled: value })
    ElMessage.success(value ? '已启用' : '已停用')
  } finally {
    await load()
  }
}

async function sendTest(row) {
  testing.value = row.id
  try {
    const result = await testChannel(row.id)
    if (result.ok) {
      ElMessage.success('测试消息已发出，请到对应群里确认')
    } else {
      ElMessage.error(`发送失败：${result.detail}`)
    }
    await load()
  } finally {
    testing.value = null
  }
}

async function remove(row) {
  await ElMessageBox.confirm(`删除渠道「${row.name}」？历史发送记录会保留。`, '确认删除', {
    type: 'warning'
  })
  await deleteChannel(row.id)
  ElMessage.success('已删除')
  await load()
}

async function loadLogs() {
  logLoading.value = true
  try {
    const page = await listNotificationLogs({
      status: logFilter.value || undefined,
      limit: 100
    })
    logs.value = page.items
  } finally {
    logLoading.value = false
  }
}

async function openLogs() {
  logsVisible.value = true
  await loadLogs()
}

onMounted(async () => {
  await Promise.all([load(), loadMeta()])
  await loadLogs()
})
</script>

<style scoped>
.sub {
  color: #909399;
  font-size: 12px;
}
.error {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 180px;
}
</style>
