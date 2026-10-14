<template>
  <div>
    <el-card shadow="never" class="page-card">
      <div class="toolbar">
        <el-button type="primary" :disabled="!auth.canOperate" @click="openCreate">
          <el-icon><Plus /></el-icon>新增凭据
        </el-button>
        <div class="spacer" />
        <el-text type="info" size="small">
          口令只写不读：保存后接口不会再返回明文，界面上也永远看不到
        </el-text>
      </div>

      <el-table v-loading="loading" :data="profiles" empty-text="还没有凭据">
        <el-table-column prop="name" label="名称" min-width="140" />
        <el-table-column prop="username" label="用户名" min-width="120" />
        <el-table-column label="口令" width="100">
          <template #default="{ row }">
            <el-tag :type="row.has_password ? 'success' : 'info'" size="small">
              {{ row.has_password ? '已设置' : '未设置' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="enable 口令" width="120">
          <template #default="{ row }">
            <el-tag :type="row.has_enable_password ? 'success' : 'info'" size="small">
              {{ row.has_enable_password ? '已设置' : '无' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="说明" min-width="160" show-overflow-tooltip />
        <el-table-column label="操作" width="140">
          <template #default="{ row }">
            <el-button link type="primary" size="small" :disabled="!auth.canOperate" @click="openEdit(row)">
              编辑
            </el-button>
            <el-popconfirm
              title="删除后使用该凭据的设备将无法采集，确定？"
              @confirm="remove(row)"
            >
              <template #reference>
                <el-button link type="danger" size="small" :disabled="!auth.canAdmin">删除</el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑凭据' : '新增凭据'" width="480px">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="110px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="例如：华为默认账号" />
        </el-form-item>
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="设备登录用户名" />
        </el-form-item>
        <el-form-item label="口令">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            :placeholder="editing ? '留空表示不修改' : '设备登录口令'"
          />
        </el-form-item>
        <el-form-item label="enable 口令">
          <el-input
            v-model="form.enable_password"
            type="password"
            show-password
            :placeholder="editing ? '留空表示不修改' : '锐捷/华三提权口令，没有可留空'"
          />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  createCredential,
  deleteCredential,
  listCredentials,
  updateCredential
} from '../api/endpoints'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const loading = ref(false)
const saving = ref(false)
const profiles = ref([])
const dialogVisible = ref(false)
const editing = ref(null)
const formRef = ref()

const form = reactive({
  name: '',
  username: '',
  password: '',
  enable_password: '',
  description: ''
})

const rules = {
  name: [{ required: true, message: '请填写名称', trigger: 'blur' }],
  username: [{ required: true, message: '请填写用户名', trigger: 'blur' }]
}

async function load() {
  loading.value = true
  try {
    profiles.value = await listCredentials()
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  Object.assign(form, { name: '', username: '', password: '', enable_password: '', description: '' })
  dialogVisible.value = true
}

function openEdit(row) {
  editing.value = row
  Object.assign(form, {
    name: row.name,
    username: row.username,
    password: '',
    enable_password: '',
    description: row.description || ''
  })
  dialogVisible.value = true
}

async function submit() {
  await formRef.value.validate()
  saving.value = true
  try {
    if (editing.value) {
      // 留空 = 不改。注意不能传 null，后端把 null 当"不改"、空串当"清空"处理
      const payload = { name: form.name, username: form.username, description: form.description }
      if (form.password) payload.password = form.password
      if (form.enable_password) payload.enable_password = form.enable_password
      await updateCredential(editing.value.id, payload)
    } else {
      await createCredential({ ...form })
    }
    ElMessage.success('已保存')
    dialogVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function remove(row) {
  await deleteCredential(row.id)
  ElMessage.success('已删除')
  await load()
}

onMounted(load)
</script>
