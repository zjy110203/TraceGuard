<script setup lang="ts">
// Consistent empty/zero-data state. An empty page is a product statement, not a
// blank div: it says what is missing and (optionally) offers the next action.
import AppIcon from './AppIcon.vue'

withDefaults(
  defineProps<{ title: string; hint?: string; icon?: string; compact?: boolean }>(),
  { icon: 'layers', compact: false },
)
</script>

<template>
  <div class="empty" :class="{ compact }">
    <span class="empty-icon"><AppIcon :name="icon" :size="compact ? 18 : 22" /></span>
    <p class="empty-title">{{ title }}</p>
    <p v-if="hint" class="empty-hint">{{ hint }}</p>
    <div class="empty-action"><slot /></div>
  </div>
</template>

<style scoped>
.empty-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: 12px;
  background: var(--tg-panel-2);
  border: 1px solid var(--tg-border);
  color: var(--tg-muted);
}

.empty.compact {
  padding: 18px 12px;
}

.empty.compact .empty-icon {
  width: 32px;
  height: 32px;
  border-radius: 9px;
}

.empty-action:empty {
  display: none;
}
</style>
