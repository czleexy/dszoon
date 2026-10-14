<template>
  <div>
    <div class="toolbar">
      <el-tag type="success" size="small">新增 {{ diff.added }}</el-tag>
      <el-tag type="danger" size="small">删除 {{ diff.removed }}</el-tag>
      <el-tag type="warning" size="small">修改 {{ diff.changed }}</el-tag>
      <el-tag :type="diff.normalized ? 'info' : 'warning'" size="small">
        {{ diff.normalized ? '已归一化后比对' : '原文比对' }}
      </el-tag>
      <div class="spacer" />
      <el-text type="info" size="small">
        {{ formatTime(diff.from_captured_at) }} → {{ formatTime(diff.to_captured_at) }}
      </el-text>
    </div>

    <el-alert
      v-if="diff.identical"
      type="success"
      :closable="false"
      show-icon
      title="两个版本内容完全一致"
    />

    <template v-else>
      <el-card v-if="diff.significant?.length" shadow="never" class="page-card">
        <template #header>
          <el-icon><WarningFilled /></el-icon>
          重要变更（{{ diff.significant.length }}）
        </template>
        <div v-for="(item, index) in diff.significant" :key="index" class="mono significant">
          {{ item }}
        </div>
      </el-card>

      <div class="diff-body mono">
        <div
          v-for="(line, index) in lines"
          :key="index"
          class="diff-line"
          :class="lineClass(line)"
        >{{ line || ' ' }}</div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { formatTime } from '../utils/format'

const props = defineProps({
  diff: { type: Object, required: true }
})

const lines = computed(() => (props.diff.unified || '').split('\n'))

function lineClass(line) {
  if (line.startsWith('@@')) return 'hunk'
  if (line.startsWith('+++') || line.startsWith('---')) return 'file-header'
  if (line.startsWith('+')) return 'added'
  if (line.startsWith('-')) return 'removed'
  return ''
}
</script>

<style scoped>
.diff-body {
  margin: 0;
  max-height: 620px;
  overflow: auto;
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 12px;
  border-radius: 4px;
  line-height: 1.5;
}
/* 每行一个 div + white-space: pre：避免 <pre> 里用模板插值换行被 Vue 的
   whitespace: 'condense' 折叠掉（那样整个 diff 会挤成一行） */
.diff-line {
  white-space: pre;
  padding: 0 4px;
}
.diff-line.added {
  background: #1d3a1d;
  color: #b5f5b5;
}
.diff-line.removed {
  background: #3d1d1d;
  color: #f5b5b5;
}
.diff-line.hunk {
  color: #6ab0f3;
}
.diff-line.file-header {
  color: #c586c0;
  font-weight: 600;
}
.significant {
  padding: 2px 0;
  color: #b88230;
}
</style>
