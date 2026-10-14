<template>
  <div>
    <el-card shadow="never" class="page-card">
      <div class="toolbar">
        <el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon>新增用户</el-button>
        <div class="spacer" />
        <el-text type="info" size="small">
          角色是包含式的：管理员 &gt; 运维 &gt; 审计 &gt; 只读；「审计」可查看配置明文
        </el-text>
      </div>

      <el-table v-loading="loading" :data="users">
        <el-table-column prop="id" label="#" width="70" />
        <el-table-column prop="username" label="用户名" min-width="140" />
        <el-table-column prop="display_name" label="姓名" min-width="120" />
        <el-table-column label="角色" width="140">
          <template #default="{ row }">
            <el-select
              :model-value="row.role"
              size="small"
              style="width: 110px"
              @change="(value) => changeRole(row, value)"
            >
              <el-option label="管理员" value="admin" />
              <el-option label="运维" value="operator" />
              <el-option label="审计" value="auditor" />
              <el-option label="只读" value="viewer" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-switch
              :model-value="row.is_active"
              @change="(value) => toggleActive(row, value)"
            />
          </template>
        </el-table-column>
        <el-table-column label="最近登录" width="170">
          <template #default="{ row }">{{ formatTime(row.last_login_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="130">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openReset(row)">重置口令</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="createVisible" title="新增用户" width="420px">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" />
        </el-form-item>
        <el-form-item label="口令" prop="password">
          <el-input v-model="form.password" type="password" show-password placeholder="至少 8 位" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role" style="width: 100%">
            <el-option label="管理员" value="admin" />
            <el-option label="运维" value="operator" />
            <el-option label="审计" value="auditor" />
            <el-option label="只读" value="viewer" />
          </el-select>
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="form.display_name" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitCreate">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="resetVisible" :title="`重置 ${current?.username || ''} 的口令`" width="420px">
      <el-input v-model="newPassword" type="password" show-password placeholder="新口令（至少 8 位）" />
      <template #footer>
        <el-button @click="resetVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitReset">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { createUser, listUsers, updateUser } from '../api/endpoints'
import { formatTime } from '../utils/format'

// 权限由路由守卫兜底（meta.role = admin），这里不需要再判一次
const loading = ref(false)
const saving = ref(false)
const users = ref([])
const createVisible = ref(false)
const resetVisible = ref(false)
const current = ref(null)
const newPassword = ref('')
const formRef = ref()
const form = reactive({ username: '', password: '', role: 'viewer', display_name: '' })

const rules = {
  username: [{ required: true, min: 2, message: '用户名至少 2 位', trigger: 'blur' }],
  password: [{ required: true, min: 8, message: '口令至少 8 位', trigger: 'blur' }]
}

async function load() {
  loading.value = true
  try {
    users.value = (await listUsers({ limit: 200 })).items
  } finally {
    loading.value = false
  }
}

function openCreate() {
  Object.assign(form, { username: '', password: '', role: 'viewer', display_name: '' })
  createVisible.value = true
}

async function submitCreate() {
  await formRef.value.validate()
  saving.value = true
  try {
    await createUser({ ...form })
    ElMessage.success('已创建')
    createVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function changeRole(row, role) {
  try {
    await updateUser(row.id, { role })
    ElMessage.success('角色已更新')
    await load()
  } catch {
    await load()
  }
}

async function toggleActive(row, value) {
  try {
    await updateUser(row.id, { is_active: value })
    ElMessage.success(value ? '已启用' : '已停用')
  } catch {
    // 失败时刷新回真实状态
  } finally {
    await load()
  }
}

function openReset(row) {
  current.value = row
  newPassword.value = ''
  resetVisible.value = true
}

async function submitReset() {
  if (newPassword.value.length < 8) {
    ElMessage.warning('口令至少 8 位')
    return
  }
  saving.value = true
  try {
    await updateUser(current.value.id, { new_password: newPassword.value })
    ElMessage.success('口令已重置')
    resetVisible.value = false
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>
