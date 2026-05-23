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
    <div className="rounded-xl border border-theme-border/50 bg-theme-base p-4">
      <p className="text-[10px] font-medium uppercase tracking-[0.2em] text-theme-muted">{label}</p>
      <p
        className={`mt-2 font-mono text-3xl font-bold tabular-nums ${
          flash ? 'animate-token-flash text-emerald-500' : 'text-emerald-500'
        }`}
      >
        {formatTokenCount(value)}
      </p>
    </div>
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
    <section className="rounded-2xl bg-theme-surface/45 p-5 shadow-sm ring-1 ring-theme-border/25 dark:bg-theme-surface/20">
      <h2 className="text-xs font-medium uppercase tracking-[0.25em] text-theme-muted">
        Token Counter — Fuel Gauge
      </h2>
      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <TokenReadout label="Prompt Tokens" value={promptTokens} flash={flashing.prompt} />
        <TokenReadout
          label="Completion Tokens"
          value={completionTokens}
          flash={flashing.completion}
        />
        <TokenReadout label="Session Total" value={sessionTotal} flash={flashing.total} />
      </div>
    </section>
  )
}
