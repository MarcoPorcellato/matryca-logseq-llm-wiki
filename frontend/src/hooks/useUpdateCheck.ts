import { useEffect, useState } from 'react'

import type { UpdateCheckResponse } from '../types/daemon'

const API_BASE =
  import.meta.env.VITE_API_BASE ?? (import.meta.env.DEV ? 'http://127.0.0.1:8000' : '')

export function useUpdateCheck(enabled: boolean = true) {
  const [updateInfo, setUpdateInfo] = useState<UpdateCheckResponse | null>(null)

  useEffect(() => {
    if (!enabled) {
      return
    }

    let cancelled = false

    const loadUpdateCheck = async () => {
      try {
        const response = await fetch(`${API_BASE}/api/system/update-check`, {
          headers: { Accept: 'application/json' },
        })
        if (!response.ok) {
          return
        }
        const payload = (await response.json()) as UpdateCheckResponse
        if (!cancelled) {
          setUpdateInfo(payload)
        }
      } catch {
        if (!cancelled) {
          setUpdateInfo(null)
        }
      }
    }

    void loadUpdateCheck()

    return () => {
      cancelled = true
    }
  }, [enabled])

  return updateInfo
}
