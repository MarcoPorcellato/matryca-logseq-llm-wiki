import type { DaemonStateResponse, FileStateResponse, FileStatus } from '../types/daemon'
import { basenameFromPath, computeProgressMetrics } from '../utils/metrics'

const FILE_STATUS_STYLES: Record<FileStatus, string> = {
  processed:
    'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  pending: 'border-theme-accent/50 bg-theme-accent-bg/30 text-theme-accent',
  error: 'border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300',
  skipped: 'border-theme-border/40 bg-theme-base/30 text-theme-muted',
}

const MAX_VISIBLE_PILLS = 14

function FileStatusPill({ path, file }: { path: string; file: FileStateResponse }) {
  const label = basenameFromPath(path)?.replace(/\.md$/i, '') ?? path
  return (
    <span
      className={`inline-flex max-w-full items-center rounded-full border px-2.5 py-1 font-mono text-[10px] font-medium uppercase tracking-wide ${FILE_STATUS_STYLES[file.status]}`}
      title={`${label} — ${file.status}${file.error ? `: ${file.error}` : ''}`}
    >
      <span className="truncate">{label}</span>
    </span>
  )
}

function buildFilePills(state: DaemonStateResponse) {
  return Object.entries(state.files)
    .sort(([, left], [, right]) => right.processed_at.localeCompare(left.processed_at))
    .slice(0, MAX_VISIBLE_PILLS)
}

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
    <section className="rounded-2xl border border-theme-border/25 bg-theme-surface/45 p-5 shadow-sm transition-all duration-300 dark:bg-theme-surface/20">
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

      {state && (
        <div className="mt-4 flex min-h-[4.5rem] flex-wrap content-start gap-2">
          {buildFilePills(state).map(([path, file]) => (
            <FileStatusPill key={path} path={path} file={file} />
          ))}
          {Object.keys(state.files).length === 0 && (
            <span className="text-[10px] text-theme-muted">
              No file checkpoints yet — start the engine to populate processing pills.
            </span>
          )}
          {Object.keys(state.files).length > MAX_VISIBLE_PILLS && (
            <span className="self-center text-[10px] text-theme-muted">
              +{Object.keys(state.files).length - MAX_VISIBLE_PILLS} more
            </span>
          )}
        </div>
      )}

      {state && !state.bootstrap_complete && state.phase2_llm_turns > 0 && (
        <p className="mt-1 text-[10px] text-theme-muted">
          Phase 2 turns queued: {state.phase2_llm_turns}
        </p>
      )}
    </section>
  )
}
