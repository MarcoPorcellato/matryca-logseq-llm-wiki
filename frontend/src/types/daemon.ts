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

/** Mirrors ``GraphAnalyticsResponse`` from ``src/cli/ui_server.py``. */
export interface GraphAnalytics {
  total_pages: number
  total_journals: number
  total_links: number
  human_pages: number
  human_links: number
  ai_pages: number
  ai_links: number
  ai_blocks_healed: number
  alias_count: number
  semantic_links: number
  semantic_cache_mb: number
  context_acceleration: number
}

export const DEFAULT_GRAPH_ANALYTICS: GraphAnalytics = {
  total_pages: 0,
  total_journals: 0,
  total_links: 0,
  human_pages: 0,
  human_links: 0,
  ai_pages: 0,
  ai_links: 0,
  ai_blocks_healed: 0,
  alias_count: 0,
  semantic_links: 0,
  semantic_cache_mb: 0,
  context_acceleration: 94.2,
}

export interface DaemonImpactCounters {
  ai_pages_created: number
  ai_links_injected: number
  ai_blocks_healed: number
  hygiene_corrections: number
}

export const DEFAULT_IMPACT_COUNTERS: DaemonImpactCounters = {
  ai_pages_created: 0,
  ai_links_injected: 0,
  ai_blocks_healed: 0,
  hygiene_corrections: 0,
}

function readNumber(source: Record<string, unknown>, snakeKey: string, camelKey: string): number {
  const value = source[snakeKey] ?? source[camelKey]
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value
  }
  if (typeof value === 'string' && value.trim().length > 0) {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) {
      return parsed
    }
  }
  return 0
}

function hasExplicitField(source: Record<string, unknown>, snakeKey: string, camelKey: string): boolean {
  return snakeKey in source || camelKey in source
}

/** True when the API payload includes an explicit graph analytics object (not just UI defaults). */
export function hasGraphAnalyticsPayload(raw: unknown): boolean {
  if (!raw || typeof raw !== 'object') {
    return false
  }
  const source = raw as Record<string, unknown>
  return (
    'total_pages' in source ||
    'totalPages' in source ||
    'semantic_links' in source ||
    'semanticLinks' in source ||
    'human_links' in source ||
    'humanLinks' in source
  )
}

/** Coerce `/api/state` payloads into the snake_case contract consumed by the UI. */
export function normalizeGraphAnalytics(raw: unknown): GraphAnalytics {
  if (!hasGraphAnalyticsPayload(raw)) {
    return { ...DEFAULT_GRAPH_ANALYTICS }
  }
  const source = raw as Record<string, unknown>
  const contextAcceleration = readNumber(source, 'context_acceleration', 'contextAcceleration')
  const totalPages = readNumber(source, 'total_pages', 'totalPages')
  const totalLinks = readNumber(source, 'total_links', 'totalLinks')
  const semanticLinks = readNumber(source, 'semantic_links', 'semanticLinks')
  const resolvedTotalLinks = totalLinks > 0 ? totalLinks : semanticLinks
  const aiPages = readNumber(source, 'ai_pages', 'aiPages')
  const aiLinks = readNumber(source, 'ai_links', 'aiLinks')
  const humanPages = hasExplicitField(source, 'human_pages', 'humanPages')
    ? readNumber(source, 'human_pages', 'humanPages')
    : Math.max(0, totalPages - aiPages)
  const humanLinks = hasExplicitField(source, 'human_links', 'humanLinks')
    ? readNumber(source, 'human_links', 'humanLinks')
    : Math.max(0, resolvedTotalLinks - aiLinks)
  return {
    total_pages: totalPages,
    total_journals: readNumber(source, 'total_journals', 'totalJournals'),
    total_links: resolvedTotalLinks,
    human_pages: humanPages,
    human_links: humanLinks,
    ai_pages: aiPages,
    ai_links: aiLinks,
    ai_blocks_healed: readNumber(source, 'ai_blocks_healed', 'aiBlocksHealed'),
    alias_count: readNumber(source, 'alias_count', 'aliasCount'),
    semantic_links: semanticLinks > 0 ? semanticLinks : resolvedTotalLinks,
    semantic_cache_mb: readNumber(source, 'semantic_cache_mb', 'semanticCacheMb'),
    context_acceleration:
      contextAcceleration > 0 ? contextAcceleration : DEFAULT_GRAPH_ANALYTICS.context_acceleration,
  }
}

function normalizeImpactCounters(raw: DaemonStateResponse): DaemonImpactCounters {
  const source = raw as unknown as Record<string, unknown>
  const aiLinksInjected = hasExplicitField(source, 'ai_links_injected', 'aiLinksInjected')
    ? readNumber(source, 'ai_links_injected', 'aiLinksInjected')
    : readNumber(source, 'links_backpropagated', 'linksBackpropagated')
  const aiBlocksHealed = hasExplicitField(source, 'ai_blocks_healed', 'aiBlocksHealed')
    ? readNumber(source, 'ai_blocks_healed', 'aiBlocksHealed')
    : readNumber(source, 'blocks_healed', 'blocksHealed')
  return {
    ai_pages_created: readNumber(source, 'ai_pages_created', 'aiPagesCreated'),
    ai_links_injected: aiLinksInjected,
    ai_blocks_healed: aiBlocksHealed,
    hygiene_corrections: readNumber(source, 'hygiene_corrections', 'hygieneCorrections'),
  }
}

/** Coerce legacy `/api/state` payloads that omit ``graph_analytics`` or use camelCase keys. */
export function normalizeDaemonState(raw: DaemonStateResponse): DaemonStateResponse {
  const files =
    raw.files && typeof raw.files === 'object' && !Array.isArray(raw.files) ? raw.files : {}
  const impact = normalizeImpactCounters(raw)

  return {
    ...raw,
    files,
    ...impact,
    graph_analytics: normalizeGraphAnalytics(raw.graph_analytics),
  }
}

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
  ai_pages_created?: number
  ai_links_injected?: number
  ai_blocks_healed?: number
  hygiene_corrections?: number
  graph_analytics?: GraphAnalytics
}

/** Mirrors ``PlumberConfigResponse`` from ``src/cli/ui_server.py``. */
export interface PlumberConfig {
  logseq_graph_path: string
  lm_studio_url: string
  lm_model: string
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

/** Mirrors ``LmModelsResponse`` from ``src/cli/ui_server.py``. */
export interface LmModelsResponse {
  models: string[]
  error: string | null
}

/** Mirrors ``UpdateCheckResponse`` from ``src/cli/ui_server.py``. */
export interface UpdateCheckResponse {
  current_version: string
  latest_version: string
  update_available: boolean
  pypi_url: string
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
  engineError: string | null
  startEngine: () => Promise<void>
  stopEngine: () => Promise<void>
  saveConfig: (payload: PlumberConfig) => Promise<PlumberConfig | null>
  refreshConfig: () => Promise<void>
}

export const LOW_PRIORITY_NICENESS = 19

export function isEngineActive(status: DaemonStatusValue | undefined): boolean {
  return status === 'running' || status === 'idle'
}
