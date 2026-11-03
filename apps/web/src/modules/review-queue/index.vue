<script setup lang="ts">
// 可复用业务组件：审核队列（任务列表与状态机按钮）
// 约定：只通过 props 接收数据、通过 emit 抛事件，不自己发请求
import type { Schemas } from '@/api/types'
import { relativeTime, reviewActionLabel } from '@/shared/format'

import { reviewTaskStateLabel } from './state'

const props = defineProps<{
  tasks: Schemas['ReviewTask'][]
  activeId?: string | null
  busyId?: string | null
  /** task.id → 「路线标题 · 作者」；调用方补齐，缺了就退回显示任务号 */
  labels?: Record<string, string>
}>()

/** 列表第一行优先显示人能读的标题，拿不到才退回任务号 */
function taskLabel(task: Schemas['ReviewTask']): string {
  const key = task.id ?? ''
  return props.labels?.[key] ?? key
}

const emit = defineEmits<{
  select: [task: Schemas['ReviewTask']]
  claim: [taskId: string]
  publish: [task: Schemas['ReviewTask']]
}>()

function onSelect(task: Schemas['ReviewTask']): void {
  emit('select', task)
}

function onClaim(task: Schemas['ReviewTask']): void {
  if (task.id) emit('claim', task.id)
}

/** 审核通过之后还要「发布」才会出现在发现页，这里给出入口 */
function canPublish(task: Schemas['ReviewTask']): boolean {
  return task.state === 'COMPLETED' && task.completed_action === 'approve'
}

function onPublish(task: Schemas['ReviewTask']): void {
  emit('publish', task)
}
</script>

<template>
  <ul class="queue">
    <li
      v-for="task in tasks"
      :key="task.id"
      class="task"
      :class="{ active: task.id === activeId }"
      role="button"
      tabindex="0"
      @click="onSelect(task)"
      @keydown.enter.prevent="onSelect(task)"
    >
      <div class="row task-head">
        <span class="task-title">{{ taskLabel(task) }}</span>
        <span class="badge" :class="'state-' + (task.state ?? '').toLowerCase()">
          {{ reviewTaskStateLabel(task.state) }}
        </span>
        <span v-if="task.completed_action" class="badge">
          {{ reviewActionLabel(task.completed_action) }}
        </span>
        <span class="muted when">{{ relativeTime(task.created_at) }}</span>
        <button
          v-if="task.state === 'OPEN'"
          class="btn ghost small"
          type="button"
          :disabled="busyId === task.id"
          @click.stop="onClaim(task)"
        >
          {{ busyId === task.id ? '领取中…' : '领取' }}
        </button>
        <button
          v-if="canPublish(task)"
          class="btn small"
          type="button"
          :disabled="busyId === task.id"
          @click.stop="onPublish(task)"
        >
          {{ busyId === task.id ? '发布中…' : '发布上线' }}
        </button>
      </div>
      <p class="muted mono submission">
        任务 {{ (task.id ?? '').slice(0, 8) }} · 提交 {{ (task.submission_id ?? '—').slice(0, 8) }}
      </p>
    </li>
  </ul>
</template>

<style scoped>
.queue {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.task {
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: #fff;
  cursor: pointer;
}

.task:hover {
  border-color: var(--brand);
}

.task.active {
  border-color: var(--brand);
  box-shadow: 0 0 0 2px rgba(140, 59, 46, 0.12);
}

.task-head {
  flex-wrap: wrap;
}

.when {
  margin-left: auto;
}

.task-title {
  font-weight: 600;
}

.submission {
  margin: 6px 0 0;
}

.state-open {
  color: #8a5a00;
  background: #fdf1d8;
}

.state-claimed {
  color: #1c4c8a;
  background: #e4eefb;
}

.state-completed {
  color: #14622a;
  background: #e6f5ea;
}

.state-cancelled {
  color: var(--muted);
  background: #f1ece5;
}
</style>
