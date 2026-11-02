<script setup lang="ts">
/**
 * 接口还没接入的页面顶部提示。
 * 不做成全局横幅：同一页面上半截是真接口、下半截是演示数据时，提示要贴住那一块。
 */
withDefaults(
  defineProps<{
    /** 还在等哪个接口，会显示在提示后面，方便对照后端进度；传 null 即不显示 */
    endpoint?: string | null
  }>(),
  { endpoint: '' },
)
</script>

<template>
  <p class="demo-notice" role="note">
    演示数据，接口未接入<span v-if="endpoint" class="demo-endpoint">（等待 {{ endpoint }}）</span>
  </p>
</template>

<style scoped>
.demo-notice {
  margin: 0 0 16px;
  padding: 6px 10px;
  font-size: 12px;
  line-height: 1.5;
  color: #6b7280;
  background: #f3f4f6;
  border-radius: 8px;
}

.demo-endpoint {
  color: #9ca3af;
}
</style>
