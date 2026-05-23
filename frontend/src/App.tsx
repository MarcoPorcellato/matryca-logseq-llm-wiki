import { CognitiveProgressCard } from './components/CognitiveProgressCard'
import { FeatureStatusBar } from './components/FeatureStatusBar'
import { GraphInsightsCard } from './components/GraphInsightsCard'
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

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 overflow-auto md:grid-cols-3">
        <FeatureStatusBar
          daemonStatus={state?.status}
          config={config}
          frozen={frozen}
        />

        <TokenCounterCard
          promptTokens={state?.session_prompt_tokens ?? 0}
          completionTokens={state?.session_completion_tokens ?? 0}
        />

        <CognitiveProgressCard state={state} />

        <GraphInsightsCard state={state} />

        <LiveConsole logs={logs} />
      </div>
    </div>
  )
}
