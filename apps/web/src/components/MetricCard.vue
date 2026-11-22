<script setup lang="ts">
defineProps<{
  label: string
  value: number | string
  hint?: string
  tone?: 'default' | 'warn' | 'danger' | 'success'
}>()

const emit = defineEmits<{ open: [] }>()
</script>

<template>
  <button class="metric" :class="tone ?? 'default'" type="button" @click="emit('open')">
    <span class="label">{{ label }}</span>
    <span class="num">{{ value }}</span>
    <span v-if="hint" class="hintline muted small">{{ hint }}</span>
  </button>
</template>

<style scoped>
.metric {
  background: var(--tg-panel);
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius);
  padding: 12px 14px;
  cursor: pointer;
  text-align: left;
  display: flex;
  flex-direction: column;
  gap: 2px;
  color: var(--tg-text);
  font: inherit;
  min-width: 0;
}
.metric:hover {
  border-color: var(--tg-accent);
}
.label {
  font-size: 12px;
  color: var(--tg-muted);
}
.num {
  font-size: 22px;
  font-weight: 600;
  line-height: 1.2;
}
.hintline {
  margin-top: 2px;
}
.metric.warn .num {
  color: var(--tg-warn);
}
.metric.danger .num {
  color: var(--tg-danger);
}
.metric.success .num {
  color: var(--tg-success);
}
</style>
