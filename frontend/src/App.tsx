import { useCallback, useEffect, useState } from 'react'

import { CognitiveProgressCard } from './components/CognitiveProgressCard'
import { FeatureStatusBar } from './components/FeatureStatusBar'
import { GraphInsightsCard } from './components/GraphInsightsCard'
import { LiveConsole } from './components/LiveConsole'
import { MasterHeader } from './components/MasterHeader'
import { TokenCounterCard } from './components/TokenCounterCard'
import { usePlumberPolling } from './hooks/usePlumberPolling'

const SIDEBAR_WIDTH_STORAGE_KEY = 'matryca-plumber-sidebar-width'
const SIDEBAR_MIN_WIDTH = 240
const SIDEBAR_MAX_WIDTH = 560
const SIDEBAR_DEFAULT_WIDTH = 320

function readStoredSidebarWidth(): number {
  const raw = localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY)
  if (!raw) {
    return SIDEBAR_DEFAULT_WIDTH
  }
  const parsed = Number.parseInt(raw, 10)
  if (Number.isNaN(parsed)) {
    return SIDEBAR_DEFAULT_WIDTH
  }
  return Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, parsed))
}

function useDesktopLayout(): boolean {
  const [isDesktop, setIsDesktop] = useState(
    () => typeof window !== 'undefined' && window.matchMedia('(min-width: 1024px)').matches,
  )

  useEffect(() => {
    const mediaQuery = window.matchMedia('(min-width: 1024px)')
    const syncLayout = () => setIsDesktop(mediaQuery.matches)
    syncLayout()
    mediaQuery.addEventListener('change', syncLayout)
    return () => mediaQuery.removeEventListener('change', syncLayout)
  }, [])

  return isDesktop
}

export default function App() {
  const {
    state,
    logs,
    config,
    connectionStatus,
    lastUpdatedAt,
    frozen,
    engineBusy,
    engineError,
    startEngine,
    stopEngine,
    saveConfig,
  } = usePlumberPolling()

  const isDesktopLayout = useDesktopLayout()
  const [sidebarWidth, setSidebarWidth] = useState(readStoredSidebarWidth)

  const beginSidebarResize = useCallback(
    (event: React.MouseEvent<HTMLDivElement>) => {
      if (!isDesktopLayout) {
        return
      }

      event.preventDefault()
      const startX = event.clientX
      const startWidth = sidebarWidth
      let latestWidth = startWidth

      const onMouseMove = (moveEvent: MouseEvent) => {
        latestWidth = Math.min(
          SIDEBAR_MAX_WIDTH,
          Math.max(SIDEBAR_MIN_WIDTH, startWidth + moveEvent.clientX - startX),
        )
        setSidebarWidth(latestWidth)
      }

      const onMouseUp = () => {
        document.removeEventListener('mousemove', onMouseMove)
        document.removeEventListener('mouseup', onMouseUp)
        document.body.style.cursor = ''
        document.body.style.userSelect = ''
        localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(latestWidth))
      }

      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
      document.addEventListener('mousemove', onMouseMove)
      document.addEventListener('mouseup', onMouseUp)
    },
    [isDesktopLayout, sidebarWidth],
  )

  return (
    <div className="flex h-screen w-full flex-col overflow-hidden bg-theme-base p-4 text-theme-text md:p-6 gap-6">
      <MasterHeader
        state={state}
        connectionStatus={connectionStatus}
        lastUpdatedAt={lastUpdatedAt}
        config={config}
        frozen={frozen}
        engineBusy={engineBusy}
        engineError={engineError}
        onStartEngine={startEngine}
        onStopEngine={stopEngine}
        onSaveConfig={saveConfig}
      />

      <div className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto overscroll-y-contain lg:flex-row lg:overflow-hidden">
        <aside
          className={`relative flex shrink-0 flex-col gap-6 lg:min-h-0 lg:overflow-hidden ${
            isDesktopLayout ? '' : 'w-full'
          }`}
          style={
            isDesktopLayout
              ? {
                  width: sidebarWidth,
                  minWidth: SIDEBAR_MIN_WIDTH,
                  maxWidth: SIDEBAR_MAX_WIDTH,
                }
              : undefined
          }
        >
          <FeatureStatusBar
            daemonStatus={state?.status}
            config={config}
            frozen={frozen}
          />

          <div className="flex min-h-[12rem] flex-1 flex-col overflow-hidden lg:min-h-0">
            <LiveConsole
              logs={logs}
              telemetry={
                <TokenCounterCard
                  promptTokens={state?.session_prompt_tokens ?? 0}
                  completionTokens={state?.session_completion_tokens ?? 0}
                />
              }
            />
          </div>

          <div
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize sidebar"
            aria-valuemin={SIDEBAR_MIN_WIDTH}
            aria-valuemax={SIDEBAR_MAX_WIDTH}
            aria-valuenow={sidebarWidth}
            onMouseDown={beginSidebarResize}
            className="group absolute top-0 -right-1 z-20 hidden h-full w-3 cursor-col-resize items-center justify-center lg:flex"
          >
            <div className="flex h-16 flex-col items-center justify-center gap-1 rounded-full px-0.5 transition group-hover:bg-theme-accent/10">
              <span className="h-1 w-1 rounded-full bg-theme-border group-hover:bg-theme-accent" />
              <span className="h-1 w-1 rounded-full bg-theme-border group-hover:bg-theme-accent" />
              <span className="h-1 w-1 rounded-full bg-theme-border group-hover:bg-theme-accent" />
            </div>
          </div>
        </aside>

        <main className="flex min-h-0 min-w-0 flex-1 flex-col gap-6 lg:overflow-hidden">
          <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
            <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
              <CognitiveProgressCard state={state} />
              <GraphInsightsCard state={state} />
            </div>
          </div>
        </main>
      </div>
    </div>
  )
}
