import { useEffect, useRef, useState } from 'react'

import { formatTokenCount } from '../utils/metrics'

interface TokenCounterCardProps {
  promptTokens: number
  completionTokens: number
}

type FlashKey = 'prompt' | 'completion' | 'total'

function TokenReadout({
  label,
  value,
  flash,
}: {
  label: string
  value: number
  flash: boolean
}) {
  return (
    <span className="inline-flex items-baseline gap-2 tabular-nums">
      <span className="text-[10px] uppercase tracking-wider text-theme-muted">{label}</span>
      <span
        className={`font-semibold text-theme-text ${
          flash ? 'animate-token-flash text-emerald-500' : 'text-emerald-500'
        }`}
      >
        {formatTokenCount(value)}
      </span>
    </span>
  )
}

export function TokenCounterCard({ promptTokens, completionTokens }: TokenCounterCardProps) {
  const sessionTotal = promptTokens + completionTokens
  const prevRef = useRef({ promptTokens, completionTokens, sessionTotal })
  const [flashing, setFlashing] = useState<Record<FlashKey, boolean>>({
    prompt: false,
    completion: false,
    total: false,
  })

  useEffect(() => {
    const prev = prevRef.current
    const nextFlash: Record<FlashKey, boolean> = {
      prompt: promptTokens > prev.promptTokens,
      completion: completionTokens > prev.completionTokens,
      total: sessionTotal > prev.sessionTotal,
    }

    if (nextFlash.prompt || nextFlash.completion || nextFlash.total) {
      setFlashing(nextFlash)
      const timer = window.setTimeout(() => {
        setFlashing({ prompt: false, completion: false, total: false })
      }, 550)
      prevRef.current = { promptTokens, completionTokens, sessionTotal }
      return () => window.clearTimeout(timer)
    }

    prevRef.current = { promptTokens, completionTokens, sessionTotal }
  }, [promptTokens, completionTokens, sessionTotal])

  return (
    <div
      role="status"
      aria-label="Session token telemetry"
      className="flex flex-row flex-wrap items-center gap-x-6 gap-y-1 rounded-t-md border-b border-theme-border/40 bg-theme-surface/50 px-4 py-2 font-mono text-xs text-theme-muted"
    >
      <span className="text-[10px] uppercase tracking-[0.2em] text-theme-muted/80">Telemetry</span>
      <TokenReadout label="Tokens In" value={promptTokens} flash={flashing.prompt} />
      <span className="hidden text-theme-border/60 sm:inline" aria-hidden>
        |
      </span>
      <TokenReadout label="Tokens Out" value={completionTokens} flash={flashing.completion} />
      <span className="hidden text-theme-border/60 sm:inline" aria-hidden>
        |
      </span>
      <TokenReadout label="Session Σ" value={sessionTotal} flash={flashing.total} />
    </div>
  )
}
