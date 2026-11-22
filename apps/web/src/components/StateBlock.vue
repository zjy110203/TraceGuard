<script setup lang="ts">
defineProps<{
  loading: boolean
  error: string
  empty?: boolean
  emptyHint?: string
  retry?: () => void
}>()
</script>

<template>
  <div v-if="loading" class="state muted">加载中…</div>
  <div v-else-if="error" class="state error">
    <span>{{ error }}</span>
    <button v-if="retry" class="btn" @click="retry()">重试</button>
  </div>
  <div v-else-if="empty" class="state muted">
    {{ emptyHint || '暂无数据' }}
  </div>
  <slot v-else />
</template>

<style scoped>
.state {
  padding: 2rem 1rem;
  font-size: 13px;
  display: flex;
  gap: 12px;
  align-items: center;
}
.muted {
  color: var(--tg-muted);
}
.error {
  color: var(--tg-danger);
}
.btn {
  border: 1px solid var(--tg-border);
  background: transparent;
  border-radius: 6px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
}
</style>
