import type { DaemonStateResponse } from '../types/daemon'
import { computeProgressMetrics } from '../utils/metrics'

interface CognitiveProgressCardProps {
  state: DaemonStateResponse | null
}

export function CognitiveProgressCard({ state }: CognitiveProgressCardProps) {
  const metrics = state
    ? computeProgressMetrics(state)
    : {
        title: 'Awaiting daemon state…',
        subtitle: '—',
        done: 0,
        total: 0,
        percent: 0,
      }

  return (
    <section className="rounded-2xl bg-theme-surface/45 p-5 shadow-sm ring-1 ring-theme-border/25 dark:bg-theme-surface/20">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-xs font-medium uppercase tracking-[0.25em] text-theme-muted">
            Cognitive Progress
          </h2>
          <p className="mt-1 text-sm text-theme-text">{metrics.title}</p>
        </div>
        <span className="text-lg font-semibold text-theme-accent">
          {metrics.percent.toFixed(1)}%
        </span>
      </div>

      <div className="relative h-4 overflow-hidden rounded-full bg-theme-base ring-1 ring-theme-border/50">
        <div
          className="absolute inset-y-0 left-0 rounded-full bg-gradient-to-r from-theme-accent/80 via-theme-accent to-emerald-500 transition-all duration-700 ease-out"
          style={{ width: `${metrics.percent}%` }}
        />
        <div
          className="absolute inset-y-0 left-0 rounded-full bg-theme-accent/20 blur-sm transition-all duration-700"
          style={{ width: `${metrics.percent}%` }}
        />
      </div>

      <p className="mt-3 text-xs text-theme-muted">{metrics.subtitle}</p>
      {state && !state.bootstrap_complete && state.phase2_llm_turns > 0 && (
        <p className="mt-1 text-[10px] text-theme-muted">
          Phase 2 turns queued: {state.phase2_llm_turns}
        </p>
      )}
    </section>
  )
}
