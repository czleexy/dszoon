<template>
  <div>
    <el-card shadow="never" class="page-card">
      <div class="toolbar">
        <el-button type="primary" :disabled="!canWrite" @click="openCreate">
          <el-icon><Plus /></el-icon>新增分组
        </el-button>
        <div class="spacer" />
        <el-text type="info" size="small">
          分组用于给定时任务圈范围（比逐台勾选实用），删分组不会删设备
        </el-text>
      </div>

      <el-table v-loading="loading" :data="groups">
        <el-table-column prop="id" label="#" width="70" />
        <el-table-column label="名称" min-width="160">
          <template #default="{ row }">
            <el-tag v-if="row.color" :color="row.color" style="color: #fff; border: none">
              {{ row.name }}
            </el-tag>
            <span v-else>{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="device_count" label="设备数" width="100" />
        <el-table-column prop="description" label="说明" min-width="180" />
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="220">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openMembers(row)">成员</el-button>
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

    <el-dialog v-model="formVisible" :title="form.id ? '编辑分组' : '新增分组'" width="460px">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="80px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" maxlength="64" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" maxlength="255" />
        </el-form-item>
        <el-form-item label="颜色">
          <el-color-picker v-model="form.color" />
          <el-text type="info" size="small" style="margin-left: 8px">仅用于界面标注</el-text>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitForm">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="membersVisible" :title="`成员设备：${current?.name || ''}`" width="620px">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="「保存成员」按整体替换处理：没勾选的设备会被移出该分组（设备本身不受影响）"
        style="margin-bottom: 12px"
      />
      <el-alert
        v-if="truncated"
        type="warning"
        :closable="false"
        show-icon
        title="设备数超过 500 台，列表被截断，无法安全地做「整体替换」"
        description="请改用下面的「追加选中设备」，或到设备页逐台调整分组。"
        style="margin-bottom: 12px"
      />
      <el-select
        v-model="selectedDeviceIds"
        multiple
        filterable
        placeholder="选择设备"
        style="width: 100%"
      >
        <el-option
          v-for="device in devices"
          :key="device.id"
          :label="`${device.name}（${device.host}）`"
          :value="device.id"
        />
      </el-select>
      <template #footer>
        <el-button @click="membersVisible = false">取消</el-button>
        <el-button :loading="saving" :disabled="!canWrite" @click="submitMembers(false)">
          追加选中设备
        </el-button>
        <el-button
          type="primary"
          :loading="saving"
          :disabled="!canWrite || truncated"
          @click="submitMembers(true)"
        >
          保存成员（整体替换）
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createGroup,
  deleteGroup,
  listDevices,
  listGroups,
  setGroupDevices,
  updateGroup
} from '../api/endpoints'
import { formatTime } from '../utils/format'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const canWrite = computed(() => auth.atLeast('operator'))
const isAdmin = computed(() => auth.canAdmin)

const loading = ref(false)
const saving = ref(false)
const groups = ref([])
const devices = ref([])
const selectedDeviceIds = ref([])
//: 设备列表被 limit 截断时为 true（此时禁用"整体替换"，避免误删成员）
const truncated = ref(false)

const formVisible = ref(false)
const membersVisible = ref(false)
const current = ref(null)
const formRef = ref()
const form = reactive({ id: null, name: '', description: '', color: null })

const rules = { name: [{ required: true, message: '请输入分组名称', trigger: 'blur' }] }

async function load() {
  loading.value = true
  try {
    groups.value = await listGroups()
  } finally {
    loading.value = false
  }
}

function openCreate() {
  Object.assign(form, { id: null, name: '', description: '', color: null })
  formVisible.value = true
}

function openEdit(row) {
  Object.assign(form, {
    id: row.id,
    name: row.name,
    description: row.description || '',
    color: row.color || null
  })
  formVisible.value = true
}

async function submitForm() {
  await formRef.value.validate()
  saving.value = true
  const payload = { name: form.name, description: form.description || null, color: form.color }
  try {
    if (form.id) {
      await updateGroup(form.id, payload)
    } else {
      await createGroup(payload)
    }
    ElMessage.success('已保存')
    formVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function openMembers(row) {
  current.value = row
  selectedDeviceIds.value = []
  truncated.value = false
  membersVisible.value = true
  // ★ 成员的预选必须问后端要（group_id 过滤），不能靠前端从"全量设备列表"里过滤：
  //   全量列表有 limit 上限，设备上千台时会被截断 → 预选不全 → "整体替换"一保存
  //   就把看不见的成员全踢出分组了。
  const [members, all] = await Promise.all([
    listDevices({ group_id: row.id, limit: 500 }),
    listDevices({ limit: 500 })
  ])
  devices.value = all.items
  selectedDeviceIds.value = members.items.map((item) => item.id)
  truncated.value = members.total > members.items.length || all.total > all.items.length
}

async function submitMembers(replace) {
  if (!selectedDeviceIds.value.length) {
    ElMessage.warning('请先选择设备')
    return
  }
  saving.value = true
  try {
    const result = await setGroupDevices(current.value.id, {
      device_ids: selectedDeviceIds.value,
      replace
    })
    ElMessage.success(
      replace ? `已保存，当前共 ${result.device_count} 台设备` : `已追加，当前共 ${result.device_count} 台设备`
    )
    membersVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function remove(row) {
  await ElMessageBox.confirm(
    `删除分组「${row.name}」？分组内的 ${row.device_count} 台设备会保留，仅解除归属。`,
    '确认删除',
    { type: 'warning' }
  )
  await deleteGroup(row.id)
  ElMessage.success('已删除')
  await load()
}

onMounted(load)
</script>
