<template>
  <div>
    <el-card shadow="never" class="page-card">
      <div class="toolbar">
        <el-button type="primary" :disabled="!auth.canOperate" @click="openCreate">
          <el-icon><Plus /></el-icon>新增设备
        </el-button>
        <el-button :disabled="!auth.canOperate" @click="importVisible = true">
          <el-icon><Upload /></el-icon>批量导入
        </el-button>
        <el-button
          type="success"
          :disabled="!auth.canOperate || !selected.length"
          @click="backupSelected"
        >
          <el-icon><Refresh /></el-icon>备份选中（{{ selected.length }}）
        </el-button>
        <div class="spacer" />
        <el-button @click="load"><el-icon><RefreshRight /></el-icon>刷新</el-button>
      </div>

      <div class="toolbar">
        <el-input
          v-model="filters.q"
          placeholder="按名称 / 管理地址 / 设备名搜索"
          style="width: 260px"
          clearable
          @keyup.enter="reload"
          @clear="reload"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-select v-model="filters.vendor" placeholder="厂商" clearable style="width: 140px" @change="reload">
          <el-option v-for="v in vendors" :key="v.name" :label="v.display_name" :value="v.name" />
        </el-select>
        <el-input v-model="filters.site" placeholder="站点" style="width: 140px" clearable @keyup.enter="reload" @clear="reload" />
        <el-checkbox v-model="filters.enabled" @change="reload">仅启用</el-checkbox>
      </div>

      <el-table
        v-loading="loading"
        :data="devices"
        row-key="id"
        empty-text="还没有设备，先新增或批量导入"
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="46" />
        <el-table-column label="名称" min-width="160">
          <template #default="{ row }">
            <el-link type="primary" @click="openDetail(row)">{{ row.name }}</el-link>
            <el-tag v-if="!row.enabled" size="small" type="info" style="margin-left: 6px">已停用</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="host" label="管理地址" width="140" />
        <el-table-column label="厂商" width="110">
          <template #default="{ row }">
            <el-tag size="small">{{ vendorLabel(row.vendor) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="site" label="站点" width="120" />
        <el-table-column prop="model" label="型号" width="150" show-overflow-tooltip />
        <el-table-column prop="os_version" label="软件版本" width="120" show-overflow-tooltip />
        <el-table-column label="最近采集" width="150">
          <template #default="{ row }">
            <el-tag v-if="row.last_status" :type="runStatusType(row.last_status)" size="small">
              {{ runStatusLabel(row.last_status) }}
            </el-tag>
            <span v-else class="muted">从未采集</span>
            <div class="muted mono">{{ formatTime(row.last_backup_at) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openDetail(row)">详情</el-button>
            <el-button link size="small" :disabled="!auth.canOperate" @click="runTest(row)">测试</el-button>
            <el-button link type="success" size="small" :disabled="!auth.canOperate" @click="runBackup(row)">
              备份
            </el-button>
            <el-button link size="small" :disabled="!auth.canOperate" @click="openEdit(row)">编辑</el-button>
            <el-popconfirm title="删除设备及其全部历史版本？" @confirm="remove(row)">
              <template #reference>
                <el-button link type="danger" size="small" :disabled="!auth.canAdmin">删除</el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        style="margin-top: 12px; justify-content: flex-end"
        layout="total, sizes, prev, pager, next"
        :total="total"
        :current-page="page"
        :page-size="pageSize"
        :page-sizes="[20, 50, 100]"
        @current-change="onPageChange"
        @size-change="onSizeChange"
      />
    </el-card>

    <!-- 新增/编辑 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑设备' : '新增设备'" width="640px">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="110px">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="台账名称" prop="name">
              <el-input v-model="form.name" placeholder="例如：总部核心交换机" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="管理地址" prop="host">
              <el-input v-model="form.host" placeholder="IP 或域名" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="厂商" prop="vendor">
              <el-select v-model="form.vendor" style="width: 100%">
                <el-option v-for="v in vendors" :key="v.name" :label="v.display_name" :value="v.name" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="端口">
              <el-input-number v-model="form.port" :min="1" :max="65535" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="凭据">
              <el-select v-model="form.credential_profile_id" clearable placeholder="选择登录凭据" style="width: 100%">
                <el-option v-for="p in profiles" :key="p.id" :label="`${p.name} (${p.username})`" :value="p.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="站点">
              <el-input v-model="form.site" placeholder="例如：总部机房" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="设备 hostname">
              <el-input v-model="form.hostname" placeholder="留空则从提示符自动识别" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="标签">
              <el-input v-model="tagsText" placeholder="逗号分隔，例如：核心,生产" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-divider content-position="left">采集参数（留空用全局默认）</el-divider>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="连接超时">
              <el-input-number v-model="form.connect_timeout" :min="1" :max="600" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="命令超时">
              <el-input-number v-model="form.command_timeout" :min="1" :max="3600" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="空闲超时">
              <el-input-number v-model="form.idle_timeout" :min="1" :max="600" style="width: 100%" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="编码">
          <el-input v-model="form.encoding" placeholder="留空自动探测；老华三/锐捷可填 gbk" style="width: 200px" />
        </el-form-item>
        <el-form-item label="兼容选项">
          <el-checkbox v-model="form.legacy_algorithms">放开老旧 SSH 算法（老设备协商失败时勾选）</el-checkbox>
          <el-checkbox v-model="form.use_paramiko">改用 paramiko 兜底会话</el-checkbox>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.notes" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 批量导入 -->
    <el-dialog v-model="importVisible" title="批量导入设备（CSV）" width="720px">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="表头列名：name, host, vendor, port, username, password, enable_password, credential_profile, site, tags, hostname"
        description="name/host/vendor 必填；vendor 支持 huawei/h3c/ruijie 及别名；同名凭据不存在时会用 username/password 创建。逐行报错，坏行不影响其它行。"
      />
      <div class="toolbar" style="margin-top: 12px">
        <input type="file" accept=".csv,text/csv" @change="onFilePicked" />
        <div class="spacer" />
        <el-text type="info" size="small">已解析 {{ importRows.length }} 行</el-text>
      </div>
      <el-input
        v-model="csvText"
        type="textarea"
        :rows="10"
        placeholder="也可以直接把 CSV 内容粘贴到这里…"
        class="mono"
        @input="parseImport"
      />
      <el-alert
        v-if="importResult"
        style="margin-top: 12px"
        :type="importResult.errors.length ? 'warning' : 'success'"
        :closable="false"
        show-icon
        :title="`新增 ${importResult.created} 台，更新 ${importResult.updated} 台，跳过 ${importResult.skipped} 行`"
      >
        <div v-for="(err, index) in importResult.errors" :key="index" class="mono">{{ err }}</div>
      </el-alert>
      <template #footer>
        <el-button @click="importVisible = false">关闭</el-button>
        <el-button type="primary" :loading="importing" :disabled="!importRows.length" @click="submitImport">
          导入 {{ importRows.length }} 行
        </el-button>
      </template>
    </el-dialog>

    <!-- 连接测试结果 -->
    <el-dialog v-model="testVisible" title="连接测试结果" width="640px">
      <el-result
        v-if="testResult"
        :icon="testResult.ok ? 'success' : 'error'"
        :title="testResult.ok ? '连接成功' : '连接失败'"
        :sub-title="testResult.ok ? `耗时 ${testResult.duration_ms} ms` : testResult.error_message || testResult.status"
      >
        <template #extra>
          <el-descriptions :column="1" border size="small" style="text-align: left">
            <el-descriptions-item label="设备名">{{ testResult.hostname || '-' }}</el-descriptions-item>
            <el-descriptions-item label="状态">{{ runStatusLabel(testResult.status) }}</el-descriptions-item>
            <el-descriptions-item label="型号">{{ testResult.facts?.model || '-' }}</el-descriptions-item>
            <el-descriptions-item label="软件版本">{{ testResult.facts?.software_version || '-' }}</el-descriptions-item>
            <el-descriptions-item v-if="testResult.warnings?.length" label="警告">
              <div v-for="(w, i) in testResult.warnings" :key="i">{{ w }}</div>
            </el-descriptions-item>
          </el-descriptions>
        </template>
      </el-result>
      <template #footer>
        <el-button @click="testVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  backupDevice,
  createBackup,
  createDevice,
  deleteDevice,
  getDevice,
  importDevices,
  listCredentials,
  listDevices,
  listVendors,
  testDevice,
  updateDevice
} from '../api/endpoints'
import { useAuthStore } from '../stores/auth'
import { formatTime, runStatusLabel, runStatusType } from '../utils/format'

const auth = useAuthStore()
const router = useRouter()

const loading = ref(false)
const saving = ref(false)
const devices = ref([])
const selected = ref([])
const vendors = ref([])
const profiles = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filters = reactive({ q: '', vendor: '', site: '', enabled: false })

const dialogVisible = ref(false)
const editing = ref(null)
const formRef = ref()
const tagsText = ref('')
const emptyForm = {
  name: '',
  host: '',
  vendor: 'huawei',
  port: 22,
  credential_profile_id: null,
  site: '',
  hostname: '',
  encoding: '',
  enabled: true,
  legacy_algorithms: false,
  use_paramiko: false,
  connect_timeout: null,
  command_timeout: null,
  idle_timeout: null,
  notes: ''
}
const form = reactive({ ...emptyForm })
const rules = {
  name: [{ required: true, message: '请填写台账名称', trigger: 'blur' }],
  host: [{ required: true, message: '请填写管理地址', trigger: 'blur' }],
  vendor: [{ required: true, message: '请选择厂商', trigger: 'change' }]
}

const importVisible = ref(false)
const importing = ref(false)
const csvText = ref('')
const importRows = ref([])
const importResult = ref(null)

const testVisible = ref(false)
const testResult = ref(null)

const vendorMap = computed(() => Object.fromEntries(vendors.value.map((v) => [v.name, v.display_name])))
function vendorLabel(name) {
  return vendorMap.value[name] || name
}

async function load() {
  loading.value = true
  try {
    const data = await listDevices({
      q: filters.q || undefined,
      vendor: filters.vendor || undefined,
      site: filters.site || undefined,
      enabled: filters.enabled ? true : undefined,
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value
    })
    devices.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

function reload() {
  page.value = 1
  load()
}

function onPageChange(value) {
  page.value = value
  load()
}

function onSizeChange(value) {
  pageSize.value = value
  page.value = 1
  load()
}

function openDetail(row) {
  router.push(`/devices/${row.id}`)
}

// 用显式处理函数而不是模板内联赋值 `(rows) => (selected = rows)`：
// 后者依赖编译器对 ref 赋值的自动解包，写成函数就没有这层隐含依赖了
function onSelectionChange(rows) {
  selected.value = rows
}

function openCreate() {
  editing.value = null
  Object.assign(form, emptyForm)
  tagsText.value = ''
  dialogVisible.value = true
}

async function openEdit(row) {
  // 取详情是为了拿到超时等只在详情里才全的字段
  const detail = await getDevice(row.id)
  editing.value = detail
  Object.assign(form, {
    ...emptyForm,
    name: detail.name,
    host: detail.host,
    vendor: detail.vendor,
    port: detail.port,
    credential_profile_id: detail.credential_profile_id,
    site: detail.site || '',
    hostname: detail.hostname || '',
    encoding: detail.encoding || '',
    enabled: detail.enabled,
    legacy_algorithms: detail.legacy_algorithms,
    use_paramiko: detail.use_paramiko,
    // ★ 必须带上真实值：这三个字段后端"为 null"表示用全局默认，
    //   如果这里写死 null 提交，保存时会把设备原有的覆盖值清掉。
    connect_timeout: detail.connect_timeout,
    command_timeout: detail.command_timeout,
    idle_timeout: detail.idle_timeout,
    notes: detail.notes || ''
  })
  tagsText.value = (detail.tags || []).join(',')
  dialogVisible.value = true
}

async function submit() {
  await formRef.value.validate()
  saving.value = true
  try {
    const payload = {
      ...form,
      tags: tagsText.value
        .split(',')
        .map((t) => t.trim())
        .filter(Boolean)
    }
    // 空字符串按"不设置"处理，避免把 '' 传给只接受数字/枚举的字段
    for (const key of ['site', 'hostname', 'encoding', 'notes']) {
      if (payload[key] === '') payload[key] = null
    }
    if (editing.value) {
      await updateDevice(editing.value.id, payload)
    } else {
      await createDevice(payload)
    }
    ElMessage.success('已保存')
    dialogVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function remove(row) {
  await deleteDevice(row.id)
  ElMessage.success('已删除')
  await load()
}

async function runTest(row) {
  testResult.value = await testDevice(row.id)
  testVisible.value = true
  if (testResult.value.ok) {
    await load()
  }
}

async function runBackup(row) {
  const task = await backupDevice(row.id)
  ElMessage.success(`任务 #${task.id} 已创建`)
  router.push('/tasks')
}

async function backupSelected() {
  const ids = selected.value.map((row) => row.id)
  try {
    await ElMessageBox.confirm(`将对选中的 ${ids.length} 台设备发起采集，是否继续？`, '确认备份', {
      type: 'warning'
    })
  } catch {
    return
  }
  const task = await createBackup({ device_ids: ids, name: `手动备份 ${ids.length} 台` })
  ElMessage.success(`任务 #${task.id} 已创建`)
  router.push('/tasks')
}

// ---- CSV 导入 ----
function parseCsv(text) {
  const rows = []
  let row = []
  let field = ''
  let inQuotes = false
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i]
    if (inQuotes) {
      if (char === '"') {
        if (text[i + 1] === '"') {
          field += '"'
          i += 1
        } else {
          inQuotes = false
        }
      } else {
        field += char
      }
    } else if (char === '"') {
      inQuotes = true
    } else if (char === ',') {
      row.push(field)
      field = ''
    } else if (char === '\n') {
      row.push(field)
      rows.push(row)
      row = []
      field = ''
    } else if (char !== '\r') {
      field += char
    }
  }
  if (field || row.length) {
    row.push(field)
    rows.push(row)
  }
  return rows.filter((cells) => cells.some((cell) => cell.trim()))
}

function parseImport() {
  importResult.value = null
  const rows = parseCsv(csvText.value)
  if (rows.length < 2) {
    importRows.value = []
    return
  }
  const header = rows[0].map((cell) => cell.trim().toLowerCase())
  importRows.value = rows.slice(1).map((cells) => {
    const record = {}
    header.forEach((key, index) => {
      const value = (cells[index] || '').trim()
      if (value) record[key] = value
    })
    if (record.port) record.port = Number(record.port)
    return record
  })
}

function onFilePicked(event) {
  const file = event.target.files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => {
    // ★ Excel 在中文 Windows 上"另存为 CSV"默认是 GBK，按 UTF-8 硬读会把
    //   中文设备名/站点名读成乱码（而且是静默乱码，导入后才发现）。
    //   策略：先严格按 UTF-8 解，失败再退回 GBK。
    const buffer = reader.result
    let text
    try {
      text = new TextDecoder('utf-8', { fatal: true }).decode(buffer)
    } catch {
      try {
        text = new TextDecoder('gbk').decode(buffer)
        ElMessage.info('文件不是 UTF-8 编码，已按 GBK 解析（Excel 导出的 CSV 常见）')
      } catch {
        text = new TextDecoder('utf-8').decode(buffer)
      }
    }
    csvText.value = text.replace(/^\uFEFF/, '') // 去掉 UTF-8 BOM，否则第一列名会带上它
    parseImport()
  }
  reader.readAsArrayBuffer(file)
  event.target.value = '' // 允许同一个文件再次选择
}

async function submitImport() {
  importing.value = true
  try {
    importResult.value = await importDevices(importRows.value)
    ElMessage.success('导入完成')
    await load()
  } finally {
    importing.value = false
  }
}

onMounted(async () => {
  const [vendorList, profileList] = await Promise.all([listVendors(), listCredentials()])
  vendors.value = vendorList
  profiles.value = profileList
  if (vendorList.length) form.vendor = vendorList[0].name
  await load()
})
</script>

<style scoped>
.muted {
  color: #909399;
  font-size: 12px;
}
</style>
