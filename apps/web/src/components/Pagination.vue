<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ page: number; pageSize: number; total: number }>()
const emit = defineEmits<{ (e: 'update:page', value: number): void }>()

const pages = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize)))
</script>

<template>
  <div v-if="total > pageSize" class="pager">
    <button class="btn" :disabled="page <= 1" @click="emit('update:page', page - 1)">上一页</button>
    <span class="muted">{{ page }} / {{ pages }} · 共 {{ total }} 条</span>
    <button class="btn" :disabled="page >= pages" @click="emit('update:page', page + 1)">下一页</button>
  </div>
</template>

<style scoped>
.pager {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 0;
  font-size: 12px;
}
.muted {
  color: var(--tg-muted);
}
.btn {
  border: 1px solid var(--tg-border);
  background: transparent;
  border-radius: 6px;
  padding: 3px 10px;
  font-size: 12px;
  cursor: pointer;
}
.btn:disabled {
  opacity: 0.4;
  cursor: default;
}
</style>
