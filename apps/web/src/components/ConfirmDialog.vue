<script setup lang="ts">
defineProps<{
  title: string
  body: string
  confirmLabel?: string
  danger?: boolean
}>()

const emit = defineEmits<{ confirm: []; cancel: [] }>()
</script>

<template>
  <div class="overlay" role="dialog" aria-modal="true" :aria-label="title">
    <div class="dialog">
      <h3>{{ title }}</h3>
      <p class="body small">{{ body }}</p>
      <div class="row">
        <button class="btn" type="button" @click="emit('cancel')">取消</button>
        <button
          class="btn"
          :class="danger ? 'danger' : 'primary'"
          type="button"
          @click="emit('confirm')"
        >
          {{ confirmLabel ?? '确认' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
}
.dialog {
  background: var(--tg-panel);
  border: 1px solid var(--tg-border);
  border-radius: var(--tg-radius);
  padding: 18px 20px;
  width: min(420px, calc(100vw - 32px));
}
h3 {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 600;
}
.body {
  margin: 0 0 16px;
  white-space: pre-line;
}
.row {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
