import type { DaemonStateResponse } from '../types/daemon'
import { hasExplicitField, readNumber, readString } from '../types/daemon'

export interface ProgressMetrics {
  title: string
  subtitle: string
  done: number
  total: number
  percent: number
}

function hasServerProgress(state: DaemonStateResponse): boolean {
  const source = state as unknown as Record<string, unknown>
  return hasExplicitField(source, 'progress_mode', 'progressMode')
}

function progressFromApi(state: DaemonStateResponse): ProgressMetrics {
  const source = state as unknown as Record<string, unknown>
  const done = readNumber(source, 'progress_done', 'progressDone')
  const total = readNumber(source, 'progress_total', 'progressTotal')
  const percentRaw = readNumber(source, 'progress_percent', 'progressPercent')
  const percent = percentRaw > 0 || total > 0 ? percentRaw : 0
  return {
    title: readString(source, 'progress_title', 'progressTitle') || 'Cognitive Progress',
    subtitle: readString(source, 'progress_subtitle', 'progressSubtitle') || '—',
    done,
    total,
    percent,
  }
}

function legacyPhase1Metrics(state: DaemonStateResponse): ProgressMetrics {
  const bootstrapTotal = state.bootstrap_total ?? 0
  const bootstrapScanned = state.bootstrap_scanned ?? 0
  if (bootstrapTotal > 0) {
    const percent = Math.min(100, (bootstrapScanned / bootstrapTotal) * 100)
    return {
      title: 'Phase 1: Cataloging Graph',
      subtitle: `${bootstrapScanned} / ${bootstrapTotal} pages indexed`,
      done: bootstrapScanned,
      total: bootstrapTotal,
      percent,
    }
  }

  const entries = Object.values(state.files)
  const processed = entries.filter((file) => file.status === 'processed').length
  const total = entries.length
  const percent = total > 0 ? Math.min(100, (processed / total) * 100) : 0
  return {
    title: 'Phase 1: Cataloging Graph',
    subtitle: `${processed} / ${total} pages indexed`,
    done: processed,
    total,
    percent,
  }
}

function legacyPhase2Metrics(state: DaemonStateResponse): ProgressMetrics {
  const vaultTotal = state.phase2_cognitive_total ?? 0
  const vaultDone = state.phase2_cognitive_done ?? 0
  if (vaultTotal > 0) {
    const percent = Math.min(100, (vaultDone / vaultTotal) * 100)
    return {
      title: 'Phase 2: Cognitive Indexing',
      subtitle: `${vaultDone} / ${vaultTotal} pages cognitively indexed`,
      done: vaultDone,
      total: vaultTotal,
      percent,
    }
  }

  const clusterId = state.current_cluster ?? '—'
  const done = state.current_cluster_files_done
  const total = state.current_cluster_files_total
  if (total > 0) {
    const percent = Math.min(100, (done / total) * 100)
    return {
      title: `Phase 2: Semantic Neighborhood Cluster [${clusterId}]`,
      subtitle: `${done} / ${total} cluster files`,
      done,
      total,
      percent,
    }
  }

  const entries = Object.values(state.files)
  const processed = entries.filter((file) => file.status === 'processed').length
  const fileTotal = entries.length
  if (fileTotal > 0) {
    const percent = Math.min(100, (processed / fileTotal) * 100)
    return {
      title: 'Phase 2: Cognitive Indexing',
      subtitle: `${processed} / ${fileTotal} pages cognitively indexed`,
      done: processed,
      total: fileTotal,
      percent,
    }
  }

  const turns = state.phase2_llm_turns
  return {
    title: 'Phase 2: Cognitive Indexing',
    subtitle: `${turns} LLM turns completed`,
    done: turns,
    total: Math.max(turns, 1),
    percent: turns > 0 ? Math.min(100, (turns / Math.max(turns, 1)) * 100) : 0,
  }
}

export function computeProgressMetrics(state: DaemonStateResponse): ProgressMetrics {
  if (hasServerProgress(state)) {
    return progressFromApi(state)
  }
  if (!state.bootstrap_complete) {
    return legacyPhase1Metrics(state)
  }
  return legacyPhase2Metrics(state)
}

export function formatTokenCount(value: number): string {
  if (value >= 1_000_000) {
    return (value / 1_000_000).toFixed(1).replace('.', ',') + 'M';
  }
  if (value >= 1_000) {
    return (value / 1_000).toFixed(1).replace('.', ',') + 'K';
  }
  return value.toString();
}

export function formatDecimalIt(value: number, digits = 1): string {
  return value.toFixed(digits).replace('.', ',')
}

export function basenameFromPath(path: string | null): string | null {
  if (!path) return null
  const segments = path.split(/[/\\]/)
  return segments[segments.length - 1] ?? path
}
