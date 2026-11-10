<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { errorMessage as readError } from '@/api/adapters'
import { fetchRoutePage } from '@/api/endpoints'
import type { Schemas } from '@/api/types'
import Skeleton from '@/shared/ui/Skeleton.vue'
import EmptyState from '@/shared/ui/EmptyState.vue'
import RouteCard from '@/modules/route-card/index.vue'
import {
  AUDIENCE_OPTIONS,
  CHANNEL_OPTIONS,
  DURATION_OPTIONS,
  EMPTY_FILTERS,
  buildRouteQuery,
  hasActiveFilter,
  queryWithTheme,
  rankThemes,
  themeFromQuery,
} from './filters'
import type { BrowseFilters } from './filters'

/** 列表响应在契约里是内联的 200，adapter 已归一成分页模型 */
const PAGE_SIZE = 6
const SEARCH_DEBOUNCE_MS = 300

const route = useRoute()
const router = useRouter()

// 深链 /?theme=亲子：进页面就把筛选摆好，刷新或直接打开都不丢
const filters = ref<BrowseFilters>({ ...EMPTY_FILTERS, theme: themeFromQuery(route.query.theme) })
const searchInput = ref('')
const items = ref<Schemas['RouteSummary'][]>([])
const themePool = ref<string[]>([])
const nextCursor = ref<string | null>(null)
const loading = ref(true)
const loadingMore = ref(false)
const errorMessage = ref('')

const themes = computed(() => rankThemes(themePool.value, filters.value.theme))
const active = computed(() => hasActiveFilter(filters.value))

let searchTimer: ReturnType<typeof setTimeout> | null = null
/** 只认最后一次请求的结果，避免快速改筛选时旧响应把新结果覆盖掉 */
let requestSeq = 0

function stopSearchTimer(): void {
  if (searchTimer) {
    clearTimeout(searchTimer)
    searchTimer = null
  }
}

watch(searchInput, (value) => {
  stopSearchTimer()
  searchTimer = setTimeout(() => {
    searchTimer = null
    filters.value.q = value.trim()
  }, SEARCH_DEBOUNCE_MS)
})

/** 回车立即搜索，不用等防抖 */
function commitSearch(): void {
  stopSearchTimer()
  filters.value.q = searchInput.value.trim()
}

watch(filters, () => { load(true) }, { deep: true })

function rememberThemes(routes: Schemas['RouteSummary'][]): void {
  const tags: string[] = []
  for (const route of routes) {
    for (const tag of route.theme_tags ?? []) if (tag) tags.push(tag)
  }
  // 保留历史见过的标签：筛完主题后 chip 不会全部消失
  themePool.value = [...themePool.value, ...tags].slice(-500)
}

async function load(reset: boolean): Promise<void> {
  const seq = ++requestSeq
  if (reset) {
    loading.value = true
    errorMessage.value = ''
  } else {
    loadingMore.value = true
  }
  const cursor = reset ? null : nextCursor.value
  const query = buildRouteQuery(filters.value, cursor, PAGE_SIZE)
  try {
    const page = await fetchRoutePage(query)
    if (seq !== requestSeq) return
    const fresh = page.items
    items.value = reset ? fresh : [...items.value, ...fresh]
    nextCursor.value = page.nextCursor
    rememberThemes(fresh)
  } catch (error) {
    if (seq !== requestSeq) return
    errorMessage.value = readError(error, '加载失败，请重试')
    if (reset) {
      items.value = []
      nextCursor.value = null
    }
  } finally {
    if (seq === requestSeq) {
      loading.value = false
      loadingMore.value = false
    }
  }
}

function clearFilters(): void {
  stopSearchTimer()
  searchInput.value = ''
  filters.value = { ...EMPTY_FILTERS }
  nextCursor.value = null
  items.value = []
}

function toggleTheme(tag: string): void {
  const next = filters.value.theme === tag ? '' : tag
  filters.value.theme = next
  // 同步写回地址栏：用 replace 不往历史里堆，退一下还是上一条路
  void router.replace({ query: queryWithTheme(route.query, next) })
}

// 浏览器前进/后退换了 ?theme 时（页面不重建）筛选也要跟着换
watch(
  () => route.query.theme,
  (raw) => {
    const theme = themeFromQuery(raw)
    if (theme !== filters.value.theme) filters.value.theme = theme
  },
)

function loadMore(): void {
  if (loadingMore.value || !nextCursor.value) return
  load(false)
}

function retry(): void {
  load(items.value.length === 0)
}

onMounted(() => { load(true) })
onBeforeUnmount(stopSearchTimer)
</script>

<template>
  <div>
    <h2>发现</h2>
    <p class="muted">按主题、时长与来源，找一条长沙的文化路线</p>

    <section class="card filter-bar">
      <div class="field">
        <label class="label" for="browse-q">搜索</label>
        <input
          id="browse-q"
          v-model="searchInput"
          class="input"
          type="search"
          placeholder="路线名、摘要或主题，回车立即搜索"
          @keydown.enter.prevent="commitSearch"
        />
      </div>

      <div class="field">
        <span class="label">主题</span>
        <div v-if="themes.length" class="chips">
          <button
            v-for="tag in themes"
            :key="tag"
            type="button"
            class="chip"
            :class="{ active: filters.theme === tag }"
            @click="toggleTheme(tag)"
          >
            {{ tag }}
          </button>
        </div>
        <p v-else class="muted">还没有可用的主题标签</p>
      </div>

      <div class="row selects">
        <label class="field">
          <span class="label">时长</span>
          <select v-model="filters.duration" class="select">
            <option v-for="option in DURATION_OPTIONS" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </label>
        <label class="field">
          <span class="label">来源</span>
          <select v-model="filters.channel" class="select">
            <option v-for="option in CHANNEL_OPTIONS" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </label>
        <label class="field">
          <span class="label">适合人群</span>
          <select v-model="filters.audience" class="select">
            <option v-for="option in AUDIENCE_OPTIONS" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </label>
      </div>

      <div class="row bar-foot">
        <button type="button" class="btn ghost small" :disabled="!active" @click="clearFilters">
          清空筛选
        </button>
        <span v-if="!loading" class="muted">共 {{ items.length }} 条</span>
      </div>
    </section>

    <div v-if="loading" class="stack" aria-busy="true" data-testid="browse-loading">
      <div v-for="index in PAGE_SIZE" :key="index" class="card">
        <Skeleton :lines="3" />
      </div>
    </div>

    <div v-else-if="errorMessage" class="card">
      <p class="error-text">{{ errorMessage }}</p>
      <button type="button" class="btn small" @click="retry">重试</button>
    </div>

    <EmptyState
      v-else-if="!items.length"
      title="没有符合条件的路线"
      hint="换个关键词，或清空筛选看看全部路线"
    >
      <button type="button" class="btn ghost small" @click="clearFilters">清空筛选</button>
    </EmptyState>

    <template v-else>
      <RouteCard v-for="route in items" :key="route.id" :route="route" />
      <div class="more">
        <button
          v-if="nextCursor"
          type="button"
          class="btn ghost small"
          :disabled="loadingMore"
          @click="loadMore"
        >
          {{ loadingMore ? '加载中…' : '加载更多' }}
        </button>
        <p v-else class="muted">已经到底了</p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.selects {
  align-items: flex-end;
  gap: 12px;
}

.selects .field {
  flex: 1;
  margin-bottom: 0;
}

.bar-foot {
  margin-top: 12px;
  justify-content: space-between;
}

.more {
  display: flex;
  justify-content: center;
  padding: 8px 0 4px;
}
</style>
