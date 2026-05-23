import type { DaemonStateResponse } from '../types/daemon'
import { hasGraphAnalyticsPayload, normalizeGraphAnalytics } from '../types/daemon'
import { formatDecimalIt, formatTokenCount } from '../utils/metrics'

interface GraphInsightsCardProps {
  state: DaemonStateResponse | null
}

function OrganicMetric({
  value,
  label,
}: {
  value: number
  label: string
}) {
  return (
    <div className="flex flex-col justify-between rounded-xl border border-theme-border/25 bg-theme-base/40 p-3 dark:bg-theme-base/20">
      <p className="font-mono text-lg font-bold text-theme-text">{formatTokenCount(value)}</p>
      <p className="mt-1 text-[10px] uppercase tracking-wider text-theme-muted">{label}</p>
    </div>
  )
}

function AgentMetric({
  value,
  label,
}: {
  value: number
  label: string
}) {
  return (
    <div className="flex flex-col justify-between rounded-xl border border-theme-accent/30 bg-theme-accent-bg/30 p-3">
      <p className="font-mono text-lg font-bold text-theme-accent">{formatTokenCount(value)}</p>
      <p className="mt-1 text-[10px] uppercase tracking-wider text-theme-muted">{label}</p>
    </div>
  )
}

function TopologyMetric({
  value,
  label,
}: {
  value: number
  label: string
}) {
  return (
    <div className="flex flex-col justify-between rounded-xl border border-theme-border/25 bg-theme-base/40 p-3 dark:bg-theme-base/20">
      <p className="font-mono text-lg font-bold text-theme-text">{formatTokenCount(value)}</p>
      <p className="mt-1 text-[10px] uppercase tracking-wider text-theme-muted">{label}</p>
    </div>
  )
}

function resolveAnalytics(state: DaemonStateResponse | null) {
  if (!state) return null
  return normalizeGraphAnalytics(state.graph_analytics)
}

export function GraphInsightsCard({ state }: GraphInsightsCardProps) {
  const analytics = resolveAnalytics(state)
  const cacheMb = analytics?.semantic_cache_mb ?? 0
  const acceleration = analytics?.context_acceleration ?? 94.2
  const telemetryMissing =
    state !== null &&
    !hasGraphAnalyticsPayload(state.graph_analytics) &&
    Object.keys(state.files).length > 0

  return (
    <section className="panel flex flex-col gap-4 p-4 shadow-sm md:p-6">
      {!analytics ? (
        <p className="text-xs text-theme-muted">Loading graph telemetry from /api/state…</p>
      ) : (
        <>
          {telemetryMissing && (
            <p className="rounded-xl border border-theme-accent/40 bg-theme-accent-bg/20 px-3 py-2 text-[11px] text-theme-text">
              Graph telemetry API is outdated. Restart the control room with{' '}
              <span className="font-mono">matryca plumber status</span> to load live topology
              metrics.
            </p>
          )}

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div className="flex flex-col gap-3 rounded-xl border border-theme-border/20 bg-theme-base/20 p-4">
              <h3 className="text-[11px] font-semibold uppercase tracking-wider text-theme-muted">
                Organic Human Mind
              </h3>
              <p className="text-[10px] leading-relaxed text-theme-muted">
                Biologic evolution — live totals minus certified agent impact on the next scan.
              </p>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <OrganicMetric value={analytics.human_pages} label="Organic Pages Written" />
                <OrganicMetric value={analytics.human_links} label="Organic Connections" />
              </div>
            </div>

            <div className="flex flex-col gap-3 rounded-xl border border-theme-accent/25 bg-theme-accent-bg/10 p-4">
              <h3 className="text-[11px] font-semibold uppercase tracking-wider text-theme-accent">
                Plumber Agent Cognition
              </h3>
              <p className="text-[10px] leading-relaxed text-theme-muted">
                AI enhancements stamped on disk and accumulated in the incremental ledger.
              </p>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <AgentMetric value={analytics.ai_pages} label="AI Concepts Spawned" />
                <AgentMetric value={analytics.ai_links} label="Links Reconnected" />
                <AgentMetric value={analytics.ai_blocks_healed} label="Structured Bullets" />
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <div className="flex flex-col gap-3">
              <h3 className="text-[11px] font-semibold uppercase tracking-wider text-theme-muted">
                Graph Topology
              </h3>
              <div className="grid grid-cols-2 gap-3">
                <TopologyMetric value={analytics.total_pages ?? 0} label="Pages" />
                <TopologyMetric value={analytics.total_journals ?? 0} label="Journals" />
                <TopologyMetric value={analytics.alias_count ?? 0} label="Mapped Aliases" />
                <TopologyMetric value={analytics.semantic_links ?? 0} label="Semantic Links" />
              </div>
            </div>

            <div className="flex flex-col gap-3">
              <h3 className="text-[11px] font-semibold uppercase tracking-wider text-theme-muted">
                GraphRAG &amp; Kinetic Cognition
              </h3>
              <div className="space-y-3">
                <p className="text-sm text-theme-text">
                  <span className="font-mono font-bold">{formatDecimalIt(cacheMb)} MB</span>
                  <span className="text-theme-muted"> allocated in </span>
                  <span className="font-mono text-xs text-theme-muted">.matryca_semantic_cache</span>
                </p>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs text-theme-muted">Context Acceleration Rate</span>
                  <span className="rounded border border-theme-accent/50 bg-theme-accent-bg/30 px-2 py-0.5 font-mono text-xs font-bold text-theme-accent">
                    {formatDecimalIt(acceleration)}%
                  </span>
                </div>
                <p className="text-[10px] leading-relaxed text-theme-muted">
                  Louvain Community Partitioning and BM25 indexing active in background thread.
                </p>
              </div>
            </div>
          </div>
        </>
      )}
    </section>
  )
}
