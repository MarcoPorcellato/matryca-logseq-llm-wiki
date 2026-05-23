import { CognitiveProgressCard } from './components/CognitiveProgressCard'
import { FeatureStatusBar } from './components/FeatureStatusBar'
import { HardeningShieldCard } from './components/HardeningShieldCard'
import { LiveConsole } from './components/LiveConsole'
import { MasterHeader } from './components/MasterHeader'
import { TokenCounterCard } from './components/TokenCounterCard'
import { usePlumberPolling } from './hooks/usePlumberPolling'

export default function App() {
  const {
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
  } = usePlumberPolling()

  return (
    <div className="mx-auto flex h-screen max-w-[1800px] flex-col gap-4 bg-theme-base p-4 text-theme-text">
      <MasterHeader
        state={state}
        connectionStatus={connectionStatus}
        lastUpdatedAt={lastUpdatedAt}
        config={config}
        frozen={frozen}
        engineBusy={engineBusy}
        onStartEngine={startEngine}
        onStopEngine={stopEngine}
        onSaveConfig={saveConfig}
      />

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 overflow-auto rounded-2xl bg-theme-surface/35 p-4 dark:bg-theme-surface/15 lg:grid-cols-2">
        <FeatureStatusBar
          daemonStatus={state?.status}
          config={config}
          frozen={frozen}
        />

        <div className="lg:col-span-2">
          <CognitiveProgressCard state={state} />
        </div>

        <TokenCounterCard
          promptTokens={state?.session_prompt_tokens ?? 0}
          completionTokens={state?.session_completion_tokens ?? 0}
        />

        <HardeningShieldCard config={config} />

        <LiveConsole logs={logs} />
      </div>
    </div>
  )
}
