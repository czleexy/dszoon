<template>
  <el-container style="height: 100%">
    <el-aside width="200px" style="background: #001529">
      <div class="brand">
        <el-icon size="20"><Switch /></el-icon>
        <span>dszoon 备份</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        background-color="#001529"
        text-color="#c9d1d9"
        active-text-color="#ffffff"
        router
      >
        <el-menu-item index="/dashboard">
          <el-icon><DataLine /></el-icon><span>看板</span>
        </el-menu-item>
        <el-menu-item index="/devices">
          <el-icon><Monitor /></el-icon><span>设备</span>
        </el-menu-item>
        <el-menu-item index="/tasks">
          <el-icon><Refresh /></el-icon><span>备份任务</span>
        </el-menu-item>
        <el-menu-item index="/credentials">
          <el-icon><Key /></el-icon><span>凭据</span>
        </el-menu-item>
        <el-menu-item index="/groups">
          <el-icon><FolderOpened /></el-icon><span>设备分组</span>
        </el-menu-item>
        <el-menu-item index="/schedules">
          <el-icon><AlarmClock /></el-icon><span>定时策略</span>
        </el-menu-item>
        <el-menu-item index="/channels">
          <el-icon><Bell /></el-icon><span>通知渠道</span>
        </el-menu-item>
        <el-menu-item index="/retention">
          <el-icon><Delete /></el-icon><span>保留策略</span>
        </el-menu-item>
        <el-menu-item v-if="auth.canAdmin" index="/users">
          <el-icon><User /></el-icon><span>用户</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="header">
        <span class="title">{{ currentTitle }}</span>
        <div class="spacer" />
        <el-dropdown @command="onCommand">
          <span class="user">
            <el-icon><UserFilled /></el-icon>
            {{ auth.user?.username || '-' }}
            <el-tag size="small" type="info" style="margin-left: 6px">
              {{ roleLabel }}
            </el-tag>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="password">修改口令</el-dropdown-item>
              <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>

      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>

  <el-dialog v-model="passwordVisible" title="修改口令" width="420px">
    <el-form :model="passwordForm" label-width="90px">
      <el-form-item label="原口令">
        <el-input v-model="passwordForm.old_password" type="password" show-password />
      </el-form-item>
      <el-form-item label="新口令">
        <el-input v-model="passwordForm.new_password" type="password" show-password />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="passwordVisible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submitPassword">确定</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import { changePassword } from '../api/endpoints'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const activeMenu = computed(() => {
  if (route.path.startsWith('/devices')) return '/devices'
  return route.path
})
const currentTitle = computed(() => route.meta?.title || '配置备份')
const roleLabel = computed(() => {
  const labels = { admin: '管理员', operator: '运维', auditor: '审计', viewer: '只读' }
  return labels[auth.user?.role] || auth.user?.role || ''
})

const passwordVisible = ref(false)
const saving = ref(false)
const passwordForm = reactive({ old_password: '', new_password: '' })

function onCommand(command) {
  if (command === 'logout') {
    auth.logout()
    router.push({ name: 'login' })
  } else if (command === 'password') {
    passwordForm.old_password = ''
    passwordForm.new_password = ''
    passwordVisible.value = true
  }
}

async function submitPassword() {
  if (passwordForm.new_password.length < 8) {
    ElMessage.warning('新口令至少 8 位')
    return
  }
  saving.value = true
  try {
    await changePassword(passwordForm)
    ElMessage.success('口令已更新')
    passwordVisible.value = false
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 60px;
  padding: 0 16px;
  color: #fff;
  font-weight: 600;
  border-bottom: 1px solid #0b2135;
}
.header {
  display: flex;
  align-items: center;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
}
.header .title {
  font-size: 16px;
  font-weight: 600;
}
.header .spacer {
  flex: 1;
}
.header .user {
  display: flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  outline: none;
}
:deep(.el-menu) {
  border-right: none;
}
</style>
