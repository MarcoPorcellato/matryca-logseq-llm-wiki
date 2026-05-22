/** Mirrors ``FileStateResponse`` from ``src/cli/ui_server.py``. */
export type FileStatus = 'processed' | 'skipped' | 'error' | 'pending'

export interface FileStateResponse {
  mtime: number
  processed_at: string
  status: FileStatus
  error: string | null
}

/** Mirrors ``DaemonStatusValue`` from ``src/cli/ui_server.py``. */
export type DaemonStatusValue = 'running' | 'idle' | 'stopped' | 'error'

/** Mirrors ``DaemonStateResponse`` from ``src/cli/ui_server.py``. */
export interface DaemonStateResponse {
  version: number
  files: Record<string, FileStateResponse>
  status: DaemonStatusValue
  model: string
  bootstrap_complete: boolean
  session_prompt_tokens: number
  session_completion_tokens: number
  current_cluster: string | null
  current_cluster_files_total: number
  current_cluster_files_done: number
  phase2_llm_turns: number
  last_scan_at: string | null
  last_file: string | null
}

/** Mirrors ``PlumberConfigResponse`` from ``src/cli/ui_server.py``. */
export interface PlumberConfig {
  logseq_graph_path: string
  lm_studio_url: string
  low_priority_mode: boolean
  thermal_delay_bootstrap: number
  thermal_delay_cognitive: number
  mapreduce_trigger_chars: number
  mapreduce_chunk_chars: number
  context_compression: boolean
  semantic_routing: boolean
  entity_consolidation: boolean
  property_hygiene: boolean
  marpa_framework: boolean
  heal_dangling: boolean
  backpropagate_links: boolean
  enable_inline_semantic_corrections: boolean
  auto_split: boolean
}

export type ConnectionStatus = 'live' | 'connecting' | 'offline'

export interface DaemonControlResponse {
  ok: boolean
  code: string
  message: string | null
  pid: number | null
}

export interface PlumberPollSnapshot {
  state: DaemonStateResponse | null
  logs: string[]
  config: PlumberConfig | null
  connectionStatus: ConnectionStatus
  lastUpdatedAt: Date | null
  frozen: boolean
  engineBusy: boolean
  startEngine: () => Promise<void>
  stopEngine: () => Promise<void>
  saveConfig: (payload: PlumberConfig) => Promise<PlumberConfig | null>
  refreshConfig: () => Promise<void>
}

export const LOW_PRIORITY_NICENESS = 19

export function isEngineActive(status: DaemonStatusValue | undefined): boolean {
  return status === 'running' || status === 'idle'
}
