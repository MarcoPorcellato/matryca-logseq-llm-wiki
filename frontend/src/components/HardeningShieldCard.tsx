import { LOW_PRIORITY_NICENESS, type PlumberConfig } from '../types/daemon'

interface HardeningShieldCardProps {
  config: PlumberConfig | null
}

function IndicatorRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[1fr_auto] gap-4 border-b border-theme-border/50 py-2.5 last:border-b-0">
      <span className="text-xs text-theme-muted">{label}</span>
      <span className="text-xs font-medium text-theme-text">{value}</span>
    </div>
  )
}

export function HardeningShieldCard({ config }: HardeningShieldCardProps) {
  const nicenessLabel = config?.low_priority_mode
    ? `${LOW_PRIORITY_NICENESS} — Low Priority`
    : '0 — Normal Priority'

  return (
    <section className="rounded-2xl bg-theme-surface/45 p-5 shadow-sm ring-1 ring-theme-border/25 dark:bg-theme-surface/20">
      <h2 className="text-xs font-medium uppercase tracking-[0.25em] text-theme-muted">
        Hardening Shield
      </h2>
      {!config ? (
        <p className="mt-4 text-xs text-theme-muted">Loading live config from /api/config…</p>
      ) : (
        <div className="mt-4 rounded-xl border border-theme-border/50 bg-theme-base px-4">
          <IndicatorRow label="Process Niceness" value={nicenessLabel} />
          <IndicatorRow
            label="Thermal Delay (Bootstrap)"
            value={`${config.thermal_delay_bootstrap.toFixed(1)}s`}
          />
          <IndicatorRow
            label="Thermal Delay (Cognitive)"
            value={`${config.thermal_delay_cognitive.toFixed(1)}s`}
          />
          <IndicatorRow
            label="MapReduce Trigger"
            value={`${config.mapreduce_trigger_chars.toLocaleString()} chars`}
          />
          <IndicatorRow
            label="MapReduce Chunk"
            value={`${config.mapreduce_chunk_chars.toLocaleString()} chars`}
          />
        </div>
      )}
    </section>
  )
}
