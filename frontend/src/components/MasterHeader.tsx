import { Moon, Sun } from 'lucide-react'
import { useTheme } from 'next-themes'
import { useEffect, useState } from 'react'

import type { ConnectionStatus, DaemonStateResponse, PlumberConfig } from '../types/daemon'
import { isEngineActive } from '../types/daemon'
import { basenameFromPath } from '../utils/metrics'
import { SettingsDrawer } from './SettingsDrawer'

interface MasterHeaderProps {
  state: DaemonStateResponse | null
  connectionStatus: ConnectionStatus
  lastUpdatedAt: Date | null
  config: PlumberConfig | null
  frozen: boolean
  engineBusy: boolean
  onStartEngine: () => Promise<void>
  onStopEngine: () => Promise<void>
  onSaveConfig: (payload: PlumberConfig) => Promise<PlumberConfig | null>
}

const TOOLBAR_BUTTON_CLASS =
  'inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-theme-border bg-theme-base transition-all hover:border-theme-accent/60'

function ThemeToggleButton() {
  const { theme, setTheme } = useTheme()
  const [mounted, setMounted] = useState(false)

  useEffect(() => {
    setMounted(true)
  }, [])

  const isDark = theme === 'dark'

  return (
    <button
      type="button"
      onClick={() => setTheme(isDark ? 'light' : 'dark')}
      className={`${TOOLBAR_BUTTON_CLASS} group`}
      aria-label={mounted ? (isDark ? 'Switch to light mode' : 'Switch to dark mode') : 'Toggle theme'}
    >
      {mounted ? (
        isDark ? (
          <Sun className="h-4 w-4 text-theme-muted transition-colors group-hover:text-theme-accent" strokeWidth={1.75} />
        ) : (
          <Moon className="h-4 w-4 text-theme-muted transition-colors group-hover:text-theme-accent" strokeWidth={1.75} />
        )
      ) : (
        <span className="h-4 w-4" aria-hidden />
      )}
    </button>
  )
}

function connectionBadge(connectionStatus: ConnectionStatus): {
  label: string
  className: string
} {
  switch (connectionStatus) {
    case 'live':
      return {
        label: 'LINKED',
        className: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-500',
      }
    case 'connecting':
      return {
        label: 'CONNECTING…',
        className: 'border-amber-500/40 bg-amber-500/10 text-amber-500 animate-pulse',
      }
    case 'offline':
      return {
        label: 'OFFLINE',
        className: 'border-red-500/40 bg-red-500/10 text-red-500',
      }
  }
}

export function MasterHeader({
  state,
  connectionStatus,
  lastUpdatedAt,
  config,
  frozen,
  engineBusy,
  onStartEngine,
  onStopEngine,
  onSaveConfig,
}: MasterHeaderProps) {
  const [drawerOpen, setDrawerOpen] = useState(false)
  const link = connectionBadge(connectionStatus)
  const daemonStatus = state?.status ?? 'stopped'
  const engineRunning = isEngineActive(daemonStatus) && !frozen
  const isRunning = daemonStatus === 'running'
  const isIdle = daemonStatus === 'idle'
  const isStopped = daemonStatus === 'stopped' || daemonStatus === 'error'

  const statusBadgeClass = frozen
    ? 'border-theme-border/50 bg-theme-base/60 text-theme-muted'
    : isRunning
      ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-500 animate-pulse-glow animate-status-pulse'
      : isIdle
        ? 'border-theme-accent/60 bg-theme-accent-bg/30 ring-1 ring-theme-accent/30 text-theme-accent'
        : isStopped
          ? 'border-red-500/40 bg-red-500/10 text-red-500'
          : 'border-theme-border/50 bg-theme-base/60 text-theme-muted'

  const statusLabel = frozen
    ? '● FROZEN'
    : isRunning
      ? '● RUNNING'
      : isIdle
        ? '● IDLE'
        : `● ${daemonStatus.toUpperCase()}`

  const phaseLabel = state?.bootstrap_complete
    ? 'Bootstrap Complete'
    : 'Phase 1: Cataloging Graph'

  const phaseClass = state?.bootstrap_complete ? 'text-theme-accent' : 'text-theme-muted'

  return (
    <>
      <header className="shrink-0 rounded-2xl bg-theme-surface/45 p-5 shadow-sm ring-1 ring-theme-border/25 dark:bg-theme-surface/20">
        <div className="flex flex-col gap-3">
          <div className="flex items-center justify-between gap-4">
            <div className="flex min-w-0 flex-1 items-center gap-3">
              <button
                type="button"
                onClick={() => setDrawerOpen(true)}
                className={`${TOOLBAR_BUTTON_CLASS} text-theme-text`}
                aria-label="Open settings"
              >
                <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.75">
                  <path d="M4 6h16M4 12h16M4 18h16" />
                </svg>
              </button>
              <img
                src="/Logo_boxed_noslogan.png"
                className="h-9 dark:hidden"
                alt="Matryca Logo"
              />
              <img
                src="/Logo_boxed_noslogan_white.png"
                className="hidden h-9 dark:block"
                alt="Matryca Logo"
              />
            </div>

            <div className="flex shrink-0 items-center gap-2">
              <ThemeToggleButton />
              <button
                type="button"
                disabled={engineBusy || engineRunning}
                onClick={() => void onStartEngine()}
                className="inline-flex items-center justify-center rounded-xl border border-theme-accent/80 bg-theme-accent px-4 py-2.5 text-sm font-medium text-theme-accent-foreground transition hover:bg-theme-accent/90 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Start Engine
              </button>
              <button
                type="button"
                disabled={engineBusy || frozen}
                onClick={() => void onStopEngine()}
                className="inline-flex h-9 items-center justify-center rounded-md border border-red-500/60 bg-red-500/10 px-4 text-xs font-medium text-red-500 transition-all hover:border-red-500 hover:bg-red-500/20 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Stop Engine
              </button>
            </div>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 pl-[44px]">
            <div className="flex flex-wrap items-center gap-3">
              <span
                className={`inline-flex items-center rounded-full border px-4 py-1.5 text-sm font-semibold tracking-wide ${statusBadgeClass}`}
              >
                {statusLabel}
              </span>
              <span className={`text-sm font-medium ${phaseClass}`}>{phaseLabel}</span>
            </div>

            <div className="flex flex-wrap items-center justify-end gap-x-3 gap-y-1">
              <span
                className={`inline-flex items-center rounded-md border px-2.5 py-1 text-xs font-medium uppercase tracking-wider ${link.className}`}
              >
                {link.label}
              </span>
              {state && (
                <p className="text-xs text-theme-muted">
                  Model <span className="text-theme-text">{state.model}</span>
                  {state.last_file && (
                    <>
                      {' · '}
                      Last <span className="text-theme-accent">{basenameFromPath(state.last_file)}</span>
                    </>
                  )}
                </p>
              )}
              {lastUpdatedAt && !frozen && (
                <p className="text-[10px] text-theme-muted">
                  Sync {lastUpdatedAt.toLocaleTimeString()}
                </p>
              )}
              {frozen && (
                <p className="text-[10px] text-theme-muted">Telemetry paused — zero client CPU</p>
              )}
            </div>
          </div>
        </div>
      </header>

      <SettingsDrawer
        open={drawerOpen}
        config={config}
        onClose={() => setDrawerOpen(false)}
        onSave={onSaveConfig}
      />
    </>
  )
}
