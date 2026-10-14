<template>
  <router-view />
</template>

<script setup>
// 根组件只负责路由出口；布局在 layouts/MainLayout.vue 里
</script>

<style>
html,
body,
#app {
  height: 100%;
  margin: 0;
}
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Microsoft YaHei', sans-serif;
  background: #f5f7fa;
}
.page-card {
  margin-bottom: 16px;
}
.toolbar {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 12px;
}
.toolbar .spacer {
  flex: 1;
}
.mono {
  font-family: Consolas, Monaco, 'Courier New', monospace;
  font-size: 12px;
}
</style>
