<template>
  <div class="login-page">
    <el-card class="login-card">
      <template #header>
        <div class="login-title">
          <el-icon size="22"><Switch /></el-icon>
          <span>dszoon 网络设备配置备份</span>
        </div>
      </template>
      <el-form :model="form" label-position="top" @keyup.enter="submit">
        <el-form-item label="用户名">
          <el-input v-model="form.username" placeholder="admin" autofocus />
        </el-form-item>
        <el-form-item label="口令">
          <el-input v-model="form.password" type="password" show-password placeholder="口令" />
        </el-form-item>
        <el-button type="primary" style="width: 100%" :loading="auth.loading" @click="submit">
          登录
        </el-button>
      </el-form>
      <el-text type="info" size="small" style="display: block; margin-top: 12px">
        首次部署的口令在服务启动日志里打印一次（或用 DSZOON_ADMIN_PASSWORD 指定）。
      </el-text>
    </el-card>
  </div>
</template>

<script setup>
import { reactive } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const form = reactive({ username: 'admin', password: '' })

async function submit() {
  if (!form.username || !form.password) {
    ElMessage.warning('请填写用户名与口令')
    return
  }
  try {
    await auth.login({ username: form.username, password: form.password })
    const redirect = route.query.redirect
    router.push(typeof redirect === 'string' && redirect ? redirect : { name: 'dashboard' })
  } catch {
    // 错误提示已由 axios 拦截器统一处理
  }
}
</script>

<style scoped>
.login-page {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #0b2135 0%, #1f4b72 100%);
}
.login-card {
  width: 380px;
}
.login-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}
</style>
