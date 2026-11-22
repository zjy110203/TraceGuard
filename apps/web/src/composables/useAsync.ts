// Loading/error handling for one backend call. Kept in one place so every page
// behaves the same: explicit loading, explicit error, explicit empty.

import { ref } from 'vue'
import type { Ref } from 'vue'
import { ApiError } from '../api/client'

export interface AsyncResult<T> {
  data: Ref<T | null>
  error: Ref<string>
  loading: Ref<boolean>
  reload: () => Promise<void>
}

export function useAsync<T>(loader: () => Promise<T>): AsyncResult<T> {
  const data = ref<T | null>(null) as Ref<T | null>
  const error = ref('')
  const loading = ref(true)

  async function reload(): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      data.value = await loader()
    } catch (err) {
      error.value = err instanceof ApiError ? err.message : String(err)
    } finally {
      loading.value = false
    }
  }

  void reload()
  return { data, error, loading, reload }
}

export function messageOf(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err)
}
