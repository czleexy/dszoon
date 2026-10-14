import { defineStore } from 'pinia'
import { fetchMe, login as loginApi } from '../api/endpoints'
import { TOKEN_KEY } from '../api/client'

const ROLE_RANK = { viewer: 10, auditor: 20, operator: 30, admin: 40 }

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem(TOKEN_KEY) || '',
    user: null,
    loading: false
  }),
  getters: {
    isLoggedIn: (state) => Boolean(state.token),
    role: (state) => state.user?.role || 'viewer',
    /** 角色是包含式的：admin > operator > auditor > viewer */
    atLeast: (state) => (minimum) => {
      const mine = ROLE_RANK[state.user?.role] || 0
      return mine >= (ROLE_RANK[minimum] || 0)
    },
    canOperate() {
      return this.atLeast('operator')
    },
    canSeePlaintext() {
      return this.atLeast('auditor')
    },
    canAdmin() {
      return this.atLeast('admin')
    }
  },
  actions: {
    async login(payload) {
      this.loading = true
      try {
        const data = await loginApi(payload)
        this.token = data.access_token
        this.user = data.user
        localStorage.setItem(TOKEN_KEY, data.access_token)
        return data
      } finally {
        this.loading = false
      }
    },
    async loadMe() {
      if (!this.token) return null
      try {
        this.user = await fetchMe()
      } catch {
        this.logout()
      }
      return this.user
    },
    logout() {
      this.token = ''
      this.user = null
      localStorage.removeItem(TOKEN_KEY)
    }
  }
})
