import { useCallback, useEffect, useRef, useState } from 'react'

import type {
  ConnectionStatus,
  DaemonControlResponse,
  DaemonStateResponse,
  GraphAnalytics,
  PlumberConfig,
  PlumberPollSnapshot,
} from '../types/daemon'
import {
  hasGraphAnalyticsPayload,
  normalizeDaemonState,
  normalizeGraphAnalytics,
} from '../types/daemon'

const POLL_INTERVAL_MS = 1000
const API_BASE =
  import.meta.env.VITE_API_BASE ?? (import.meta.env.DEV ? 'http://127.0.0.1:8000' : '')

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.headers ?? {}),
    },
  })
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`)
  }
  return (await response.json()) as T
}

/** Treat daemon start responses that should enable live UI polling. */
function isStartAccepted(result: DaemonControlResponse): boolean {
  return result.ok || result.code === 'already_running'
}

export function usePlumberPolling(intervalMs = POLL_INTERVAL_MS): PlumberPollSnapshot {
  const [state, setState] = useState<DaemonStateResponse | null>(null)
  const [logs, setLogs] = useState<string[]>([])
  const [config, setConfig] = useState<PlumberConfig | null>(null)
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('connecting')
  const [lastUpdatedAt, setLastUpdatedAt] = useState<Date | null>(null)
  const [frozen, setFrozen] = useState(true)
  const [engineBusy, setEngineBusy] = useState(false)

  const stateRef = useRef<DaemonStateResponse | null>(null)
  const logsRef = useRef<string[]>([])
  const mountedRef = useRef(true)
  const intervalRef = useRef<number | null>(null)
  const frozenRef = useRef(true)
  /** True after the operator clicks Start — keeps polling even while status is still ``stopped``. */
  const telemetryLiveRef = useRef(false)

  const clearPolling = useCallback(() => {
    if (intervalRef.current !== null) {
      window.clearInterval(intervalRef.current)
      intervalRef.current = null
    }
  }, [])

  const setTelemetryLive = useCallback(
    (live: boolean) => {
      telemetryLiveRef.current = live
      frozenRef.current = !live
      setFrozen(!live)
      if (!live) {
        clearPolling()
      }
    },
    [clearPolling],
  )

  const poll = useCallback(async () => {
    let stateOk = false
    let logsOk = false
    let configOk = false

    try {
      const refreshedConfig = await fetchJson<PlumberConfig>(`${API_BASE}/api/config`)
      if (mountedRef.current) {
        setConfig(refreshedConfig)
        configOk = true
      }
    } catch {
      if (!mountedRef.current) return
    }

    try {
      const rawState = await fetchJson<DaemonStateResponse & { graph_analytics?: unknown }>(
        `${API_BASE}/api/state`,
      )
      let graphAnalytics: GraphAnalytics | undefined = hasGraphAnalyticsPayload(rawState.graph_analytics)
        ? normalizeGraphAnalytics(rawState.graph_analytics)
        : undefined

      if (!graphAnalytics) {
        try {
          graphAnalytics = normalizeGraphAnalytics(
            await fetchJson<GraphAnalytics>(`${API_BASE}/api/graph-analytics`),
          )
        } catch {
          graphAnalytics = undefined
        }
      }

      const nextState = normalizeDaemonState({
        ...rawState,
        graph_analytics: graphAnalytics,
      })
      if (!mountedRef.current) return
      stateRef.current = nextState
      setState(nextState)
      stateOk = true
    } catch {
      if (!mountedRef.current) return
    }

    if (frozenRef.current) {
      if (!mountedRef.current) return
      if (stateOk || configOk) {
        setConnectionStatus('live')
        setLastUpdatedAt(new Date())
      } else if (!stateRef.current) {
        setConnectionStatus('offline')
      } else {
        setConnectionStatus('connecting')
      }
      return
    }

    try {
      const nextLogs = await fetchJson<string[]>(`${API_BASE}/api/logs`)
      if (!mountedRef.current) return
      const cleaned = nextLogs.filter((line) => line.trim().length > 0)
      logsRef.current = cleaned
      setLogs(cleaned)
      logsOk = true
    } catch {
      if (!mountedRef.current) return
    }

    if (!mountedRef.current) return
    if (stateOk || logsOk || configOk) {
      setConnectionStatus('live')
      setLastUpdatedAt(new Date())
    } else if (!stateRef.current) {
      setConnectionStatus('offline')
    } else {
      setConnectionStatus('connecting')
    }
  }, [])

  const startPollingLoop = useCallback(() => {
    clearPolling()
    intervalRef.current = window.setInterval(() => {
      void poll()
    }, intervalMs)
  }, [clearPolling, intervalMs, poll])

  useEffect(() => {
    mountedRef.current = true
    void poll()
    return () => {
      mountedRef.current = false
      clearPolling()
    }
  }, [clearPolling, poll])

  useEffect(() => {
    if (frozen) {
      clearPolling()
      return
    }
    void poll()
    startPollingLoop()
    return () => {
      clearPolling()
    }
  }, [clearPolling, frozen, poll, startPollingLoop])

  const refreshConfig = useCallback(async () => {
    try {
      const payload = await fetchJson<PlumberConfig>(`${API_BASE}/api/config`)
      if (mountedRef.current) {
        setConfig(payload)
      }
    } catch {
      if (mountedRef.current) {
        setConfig(null)
      }
    }
  }, [])

  useEffect(() => {
    void refreshConfig()
  }, [refreshConfig])

  const startEngine = useCallback(async () => {
    setEngineBusy(true)
    try {
      const result = await fetchJson<DaemonControlResponse>(`${API_BASE}/api/daemon/start`, {
        method: 'POST',
      })
      if (!isStartAccepted(result)) {
        return
      }
      setTelemetryLive(true)
      await poll()
    } catch {
      if (mountedRef.current) {
        setTelemetryLive(false)
      }
    } finally {
      if (mountedRef.current) {
        setEngineBusy(false)
      }
    }
  }, [poll, setTelemetryLive])

  const stopEngine = useCallback(async () => {
    setEngineBusy(true)
    setTelemetryLive(false)
    try {
      await fetchJson<DaemonControlResponse>(`${API_BASE}/api/daemon/stop`, {
        method: 'POST',
      })
      await poll()
    } finally {
      if (mountedRef.current) {
        setEngineBusy(false)
      }
    }
  }, [poll, setTelemetryLive])

  const saveConfig = useCallback(async (payload: PlumberConfig): Promise<PlumberConfig | null> => {
    try {
      const updated = await fetchJson<PlumberConfig>(`${API_BASE}/api/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (mountedRef.current) {
        setConfig(updated)
      }
      return updated
    } catch {
      return null
    }
  }, [])

  return {
    state,
    logs,
    config,
    connectionStatus,
    lastUpdatedAt,
    frozen,
    engineBusy,
    startEngine,
    stopEngine,
    saveConfig,
    refreshConfig,
  }
}
