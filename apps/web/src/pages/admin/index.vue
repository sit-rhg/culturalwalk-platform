<script setup lang="ts">
// 审核队列与发布
// 任务卡：FE-08
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import type { Schemas } from '@/api/types'
import DiffView from '@/modules/review-queue/diff-view.vue'
import ReviewQueue from '@/modules/review-queue/index.vue'
import { snapshotText, snapshotToText } from '@/modules/review-queue/diff'
import {
  canDecide,
  decisionBlockedReason,
  isPendingTask,
  reviewTaskStateLabel,
  type ReviewDecision,
} from '@/modules/review-queue/state'
import { notificationKindLabel, payloadText } from '@/pages/me/logic'
import { errorMessage as readError } from '@/api/adapters'
import {
  claimReviewTask,
  decideReviewTask,
  fetchReviewTasks,
  fetchSubmission,
  publishSubmission,
} from '@/api/endpoints'
import { api } from '@/shared/api'
import { relativeTime, reviewActionLabel } from '@/shared/format'
import { isAdmin, isLoggedIn, refreshMe } from '@/shared/session'
import EmptyState from '@/shared/ui/EmptyState.vue'
import Modal from '@/shared/ui/Modal.vue'
import Skeleton from '@/shared/ui/Skeleton.vue'

type Notification = Schemas['Notification']
type ReviewTask = Schemas['ReviewTask']
type SubmissionDetail = Schemas['SubmissionDetail']

const tasks = ref<ReviewTask[]>([])
const loading = ref(true)
const listError = ref('')
const busyId = ref<string | null>(null)
const notice = ref('')
/** 已发布成功的 submission_id：发布后按钮要立刻消失，否则重试必 409 */
const publishedSubmissionIds = ref<string[]>([])

/** 审核通知走真接口（GET /notifications），审核任务本身还是演示数据 */
const notices = ref<Notification[]>([])
const noticesLoading = ref(true)
const noticesError = ref('')

const activeTask = ref<ReviewTask | null>(null)
const submission = ref<SubmissionDetail | null>(null)
/** 任务 id → 「路线标题 · 作者」，让队列第一行是可读的标题而不是 UUID */
const labels = ref<Record<string, string>>({})
const detailLoading = ref(false)
const detailError = ref('')

const modalAction = ref<ReviewDecision | null>(null)
const note = ref('')
const actionError = ref('')
const submitting = ref(false)

const pendingTasks = computed(() => tasks.value.filter((task) => isPendingTask(task)))
const doneTasks = computed(() => tasks.value.filter((task) => !isPendingTask(task)))

const submissionTitle = computed(() => snapshotText(submission.value?.snapshot, 'title') || '未命名路线')
const authorName = computed(() => submission.value?.author?.username ?? '未知作者')
const previousContent = computed(() =>
  snapshotToText(submission.value?.previous_snapshot, submission.value?.place_names),
)
const currentContent = computed(() =>
  snapshotToText(submission.value?.snapshot, submission.value?.place_names),
)
const noteError = computed(() => decisionBlockedReason(modalAction.value, note.value))

function errorMessage(error: unknown): string {
  return readError(error, '操作失败，请稍后重试')
}

function upsert(task: ReviewTask): void {
  const index = tasks.value.findIndex((item) => item.id === task.id)
  if (index >= 0) tasks.value[index] = { ...tasks.value[index], ...task }
  else tasks.value = [task, ...tasks.value]
}

async function loadTasks(): Promise<void> {
  if (!isAdmin.value) {
    loading.value = false
    return
  }
  loading.value = true
  listError.value = ''
  try {
    tasks.value = await fetchReviewTasks()
    void loadTaskLabels()
  } catch (error) {
    listError.value = errorMessage(error)
  } finally {
    loading.value = false
  }
}

/**
 * 审核任务只带 id；标题和作者要再查一次投稿详情。
 * 队列一般只有个位数条，逐条查比让契约多一个字段更省事；失败就当作没有标签。
 */
async function loadTaskLabels(): Promise<void> {
  const pending = tasks.value.filter((task) => task.submission_id).slice(0, 20)
  const entries = await Promise.all(
    pending.map(async (task) => {
      try {
        const detail = await fetchSubmission(task.submission_id as string)
        const title = snapshotText(detail.snapshot, 'title') || '未命名路线'
        const author = detail.author?.username ?? '未知作者'
        return [task.id ?? '', `${title} · ${author}`] as const
      } catch {
        return [task.id ?? '', ''] as const
      }
    })
  )
  const next: Record<string, string> = { ...labels.value }
  for (const [id, label] of entries) {
    if (id && label) next[id] = label
  }
  labels.value = next
}

async function loadNotices(): Promise<void> {
  if (!isLoggedIn.value) {
    noticesLoading.value = false
    return
  }
  noticesLoading.value = true
  noticesError.value = ''
  try {
    const data = await api.get<{ items: Notification[] }>('/notifications?limit=20')
    notices.value = data.items ?? []
  } catch (error) {
    noticesError.value = errorMessage(error)
  } finally {
    noticesLoading.value = false
  }
}

async function claim(taskId: string): Promise<void> {
  busyId.value = taskId
  listError.value = ''
  try {
    const claimed = await claimReviewTask(taskId)
    upsert(claimed)
    if (activeTask.value?.id === taskId) activeTask.value = { ...activeTask.value, ...claimed }
    notice.value = '已领取任务 ' + taskId
  } catch (error) {
    listError.value = errorMessage(error)
  } finally {
    busyId.value = null
  }
}

/**
 * 发布上线：审核通过只代表内容没问题，还要编辑点一下才会出现在发现页。
 * 契约要求版本号 + 内容 hash，所以先查投稿详情再发。
 */
async function publish(task: ReviewTask): Promise<void> {
  const submissionId = task.submission_id
  if (!submissionId) return
  busyId.value = task.id ?? null
  listError.value = ''
  try {
    const detail = await fetchSubmission(submissionId)
    if (!detail.version_id || !detail.content_hash) {
      listError.value = '这条投稿没有版本号或内容 hash，无法发布'
      return
    }
    await publishSubmission(submissionId, {
      version_id: detail.version_id,
      content_hash: detail.content_hash,
    })
    if (!publishedSubmissionIds.value.includes(submissionId)) {
      publishedSubmissionIds.value = [...publishedSubmissionIds.value, submissionId]
    }
    notice.value = '已发布上线，发现页现在能看到它'
  } catch (error) {
    listError.value = errorMessage(error)
  } finally {
    busyId.value = null
  }
}

async function selectTask(task: ReviewTask): Promise<void> {
  activeTask.value = task
  submission.value = null
  detailError.value = ''
  notice.value = ''
  const submissionId = task.submission_id
  if (!submissionId) {
    detailError.value = '该任务没有关联的提交'
    return
  }
  detailLoading.value = true
  try {
    submission.value = await fetchSubmission(submissionId)
  } catch (error) {
    detailError.value = errorMessage(error)
  } finally {
    detailLoading.value = false
  }
}

function openModal(action: ReviewDecision): void {
  modalAction.value = action
  note.value = ''
  actionError.value = ''
}

function closeModal(): void {
  if (submitting.value) return
  modalAction.value = null
  note.value = ''
  actionError.value = ''
}

async function submitDecision(): Promise<void> {
  const task = activeTask.value
  const action = modalAction.value
  if (!task?.id || !action) return
  if (noteError.value) {
    actionError.value = noteError.value
    return
  }
  submitting.value = true
  actionError.value = ''
  try {
    // 后端字段是 action + reason（≥10 字），note 只在本地用，映射交给 adapter
    const decided = await decideReviewTask(task.id, action, note.value)
    // 后端返回的任务已 COMPLETED；再兜一层，保证它一定从待审列表消失
    upsert({ ...task, ...decided, state: 'COMPLETED', completed_action: action })
    notice.value = '已' + reviewActionLabel(action) + '，任务 ' + task.id + ' 已完成'
    modalAction.value = null
    note.value = ''
    activeTask.value = null
    submission.value = null
  } catch (error) {
    actionError.value = errorMessage(error)
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  await refreshMe()
  await Promise.all([loadTasks(), loadNotices()])
})
</script>

<template>
  <div>
    <h2>审核后台</h2>
    <p class="muted">领取待审任务，对照上一版做通过 / 退回</p>

    <EmptyState v-if="!isLoggedIn" title="请先登录" hint="审核后台需要登录后才能使用">
      <RouterLink class="btn small" to="/login">去登录</RouterLink>
    </EmptyState>

    <EmptyState v-else-if="!isAdmin" title="需要审核权限" hint="当前账号不是审核编辑，换一个身份再试">
      <RouterLink class="btn small" to="/login">用审核编辑身份登录</RouterLink>
    </EmptyState>

    <template v-else>
      <p v-if="notice" class="ok-banner">{{ notice }}</p>
      <p v-if="listError" class="error-text">{{ listError }}</p>

      <div class="section-title">审核通知</div>
      <Skeleton v-if="noticesLoading" :lines="2" />
      <p v-else-if="noticesError" class="error-text">{{ noticesError }}</p>
      <EmptyState
        v-else-if="!notices.length"
        title="暂无审核通知"
        hint="路线审核有结果后，消息会出现在这里"
      />
      <ul v-else class="notice-list">
        <li v-for="item in notices" :key="item.id" class="card notice-card">
          <div class="row notice-head">
            <strong>{{ notificationKindLabel(item.type) }}</strong>
            <span class="muted">{{ relativeTime(item.created_at) }}</span>
          </div>
          <p v-if="payloadText(item.payload, 'title')" class="notice-title">
            {{ payloadText(item.payload, 'title') }}
          </p>
          <p v-if="payloadText(item.payload, 'reason')" class="notice-reason">
            理由：{{ payloadText(item.payload, 'reason') }}
          </p>
        </li>
      </ul>

      <Skeleton v-if="loading" :lines="4" />

      <template v-else>
        <div class="section-title">待审任务（{{ pendingTasks.length }}）</div>
        <EmptyState
          v-if="!pendingTasks.length"
          title="暂无待审任务"
          hint="用户提交后，任务会出现在这里"
        />
        <ReviewQueue
          v-else
          :tasks="pendingTasks"
          :labels="labels"
          :active-id="activeTask?.id ?? null"
          :busy-id="busyId"
          :published-submission-ids="publishedSubmissionIds"
          @select="selectTask"
          @claim="claim"
          @publish="publish"
        />

        <template v-if="activeTask">
          <div class="section-title">版本对照 · {{ submissionTitle }}</div>
          <div class="card">
            <p class="muted">
              作者 {{ authorName }} · 任务 <span class="mono">{{ activeTask.id }}</span> · 状态
              {{ reviewTaskStateLabel(activeTask.state) }}
            </p>
            <Skeleton v-if="detailLoading" :lines="6" />
            <p v-else-if="detailError" class="error-text">{{ detailError }}</p>
            <DiffView v-else :previous="previousContent" :current="currentContent" />
          </div>
          <div v-if="canDecide(activeTask) && !detailLoading && !detailError" class="row actions">
            <button class="btn small" type="button" @click="openModal('approve')">通过</button>
            <button class="btn ghost small" type="button" @click="openModal('request_changes')">
              退回修改
            </button>
            <button class="btn ghost small" type="button" @click="openModal('reject')">
              拒绝
            </button>
          </div>
        </template>

        <div class="section-title">最近处理</div>
        <EmptyState v-if="!doneTasks.length" title="还没有处理记录" />
        <ReviewQueue
          v-else
          :tasks="doneTasks"
          :labels="labels"
          :published-submission-ids="publishedSubmissionIds"
          @publish="publish"
          :active-id="activeTask?.id ?? null"
          @select="selectTask"
        />
      </template>
    </template>

    <Modal :open="modalAction === 'approve'" title="确认通过" @close="closeModal">
      <p class="muted">通过后这次提交进入发布流程，备注可以先留空。</p>
      <label class="field">
        <span class="label">备注（可空，填了就至少 10 字）</span>
        <textarea v-model="note" class="textarea" placeholder="例如：内容无误，可以发布" />
      </label>
      <p v-if="noteError" class="error-text">{{ noteError }}</p>
      <p v-if="actionError" class="error-text">{{ actionError }}</p>
      <template #footer>
        <button class="btn ghost small" type="button" @click="closeModal">取消</button>
        <button
          class="btn small"
          type="button"
          :disabled="!!noteError || submitting"
          @click="submitDecision"
        >
          {{ submitting ? '提交中…' : '确认通过' }}
        </button>
      </template>
    </Modal>

    <Modal :open="modalAction === 'request_changes'" title="退回修改" @close="closeModal">
      <label class="field">
        <span class="label">退回理由（必填）</span>
        <textarea v-model="note" class="textarea" placeholder="写清楚要改什么，作者会收到通知" />
      </label>
      <p v-if="noteError" class="error-text">{{ noteError }}</p>
      <p v-if="actionError" class="error-text">{{ actionError }}</p>
      <template #footer>
        <button class="btn ghost small" type="button" @click="closeModal">取消</button>
        <button
          class="btn small"
          type="button"
          :disabled="!!noteError || submitting"
          @click="submitDecision"
        >
          {{ submitting ? '提交中…' : '确认退回' }}
        </button>
      </template>
    </Modal>

    <Modal :open="modalAction === 'reject'" title="拒绝投稿" @close="closeModal">
      <p class="muted">拒绝是终态：草稿回到编辑状态，作者会收到带理由的通知。</p>
      <label class="field">
        <span class="label">拒绝理由（必填）</span>
        <textarea v-model="note" class="textarea" placeholder="写清楚为什么不通过，作者会收到通知" />
      </label>
      <p v-if="noteError" class="error-text">{{ noteError }}</p>
      <p v-if="actionError" class="error-text">{{ actionError }}</p>
      <template #footer>
        <button class="btn ghost small" type="button" @click="closeModal">取消</button>
        <button
          class="btn small"
          type="button"
          :disabled="!!noteError || submitting"
          @click="submitDecision"
        >
          {{ submitting ? '提交中…' : '确认拒绝' }}
        </button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
.ok-banner {
  margin: 0 0 12px;
  padding: 8px 12px;
  border-radius: 10px;
  background: #e6f5ea;
  color: #14622a;
  font-size: 13px;
}

.actions {
  margin: 4px 0 12px;
}

.notice-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.notice-head {
  gap: 8px;
  justify-content: space-between;
}

.notice-title,
.notice-reason {
  margin: 6px 0 0;
  font-size: 14px;
}

.notice-reason {
  color: var(--brand);
  font-size: 13px;
}
</style>
