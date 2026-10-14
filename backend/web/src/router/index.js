import { createRouter, createWebHashHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

import MainLayout from '../layouts/MainLayout.vue'
import LoginView from '../views/LoginView.vue'
import DashboardView from '../views/DashboardView.vue'
import DevicesView from '../views/DevicesView.vue'
import DeviceDetailView from '../views/DeviceDetailView.vue'
import TasksView from '../views/TasksView.vue'
import CredentialsView from '../views/CredentialsView.vue'
import GroupsView from '../views/GroupsView.vue'
import SchedulesView from '../views/SchedulesView.vue'
import ChannelsView from '../views/ChannelsView.vue'
import RetentionView from '../views/RetentionView.vue'
import UsersView from '../views/UsersView.vue'

// 用 hash 路由是有意的：生产环境前端由 FastAPI 的 StaticFiles 提供，
// 它不会为 /devices/1 这类深链接做 SPA 回退（会 404）。
// hash 路由下所有页面都在 #/ 之后，刷新和分享链接都不会 404。
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', name: 'login', component: LoginView, meta: { public: true } },
    {
      path: '/',
      component: MainLayout,
      children: [
        { path: '', redirect: '/dashboard' },
        { path: 'dashboard', name: 'dashboard', component: DashboardView, meta: { title: '看板' } },
        { path: 'devices', name: 'devices', component: DevicesView, meta: { title: '设备' } },
        {
          path: 'devices/:id',
          name: 'device-detail',
          component: DeviceDetailView,
          props: true,
          meta: { title: '设备详情' }
        },
        { path: 'tasks', name: 'tasks', component: TasksView, meta: { title: '备份任务' } },
        {
          path: 'credentials',
          name: 'credentials',
          component: CredentialsView,
          meta: { title: '凭据' }
        },
        {
          path: 'groups',
          name: 'groups',
          component: GroupsView,
          meta: { title: '设备分组' }
        },
        {
          path: 'schedules',
          name: 'schedules',
          component: SchedulesView,
          meta: { title: '定时策略' }
        },
        {
          path: 'channels',
          name: 'channels',
          component: ChannelsView,
          meta: { title: '通知渠道' }
        },
        {
          path: 'retention',
          name: 'retention',
          component: RetentionView,
          meta: { title: '保留策略' }
        },
        {
          path: 'users',
          name: 'users',
          component: UsersView,
          meta: { title: '用户', role: 'admin' }
        }
      ]
    },
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' }
  ]
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (to.meta.public) {
    return true
  }
  if (!auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (!auth.user) {
    await auth.loadMe()
    if (!auth.isLoggedIn) {
      return { name: 'login' }
    }
  }
  if (to.meta.role && !auth.atLeast(to.meta.role)) {
    return { name: 'dashboard' }
  }
  return true
})

export default router
