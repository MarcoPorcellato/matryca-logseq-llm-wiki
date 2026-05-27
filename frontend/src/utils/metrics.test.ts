import { describe, expect, it } from 'vitest'

import type { DaemonStateResponse } from '../types/daemon'
import { computeProgressMetrics } from './metrics'

function baseState(overrides: Partial<DaemonStateResponse>): DaemonStateResponse {
  return {
    version: 1,
    files: {},
    status: 'running',
    model: 'test',
    bootstrap_complete: false,
    bootstrap_scanned: 0,
    bootstrap_total: 0,
    session_prompt_tokens: 0,
    session_completion_tokens: 0,
    current_cluster: null,
    current_cluster_files_total: 0,
    current_cluster_files_done: 0,
    phase2_llm_turns: 0,
    last_scan_at: null,
    last_file: null,
    ...overrides,
  }
}

describe('computeProgressMetrics', () => {
  it('uses API progress fields when present', () => {
    const state = baseState({
      bootstrap_complete: true,
      progress_mode: 'phase2_cluster',
      progress_title: 'Phase 2: Semantic Neighborhood Cluster [c1]',
      progress_subtitle: '2 / 5 cluster files',
      progress_done: 2,
      progress_total: 5,
      progress_percent: 40,
    })
    const metrics = computeProgressMetrics(state)
    expect(metrics.percent).toBe(40)
    expect(metrics.title).toContain('c1')
    expect(metrics.subtitle).toBe('2 / 5 cluster files')
  })

  it('falls back to vault counters for legacy phase 2 checkpoints', () => {
    const state = baseState({
      bootstrap_complete: true,
      phase2_cognitive_total: 10,
      phase2_cognitive_done: 3,
    })
    const metrics = computeProgressMetrics(state)
    expect(metrics.percent).toBe(30)
    expect(metrics.title).toContain('Cognitive Indexing')
  })

  it('reports phase 1 bootstrap counters', () => {
    const state = baseState({
      bootstrap_scanned: 10,
      bootstrap_total: 40,
    })
    const metrics = computeProgressMetrics(state)
    expect(metrics.percent).toBe(25)
    expect(metrics.done).toBe(10)
    expect(metrics.total).toBe(40)
  })
})
