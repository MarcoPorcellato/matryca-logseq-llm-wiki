import { useEffect, useState } from 'react'

import type { PlumberConfig } from '../types/daemon'

interface SettingsDrawerProps {
  open: boolean
  config: PlumberConfig | null
  onClose: () => void
  onSave: (payload: PlumberConfig) => Promise<PlumberConfig | null>
}

interface FieldSpec {
  key: keyof PlumberConfig
  label: string
  description: string
  type: 'text' | 'number' | 'boolean'
  step?: string
  badge?: string
  badgeClass?: string
}

interface TrustSection {
  id: string
  title: string
  subtitle: string
  borderClass: string
  badgeClass: string
  fields: FieldSpec[]
}

const INFRA_FIELDS: FieldSpec[] = [
  {
    key: 'logseq_graph_path',
    label: 'Logseq Graph Path',
    description: 'Root folder of your local Logseq OG markdown vault.',
    type: 'text',
  },
  {
    key: 'lm_studio_url',
    label: 'LM Studio URL',
    description: 'OpenAI-compatible endpoint for local inference.',
    type: 'text',
  },
  {
    key: 'thermal_delay_bootstrap',
    label: 'Bootstrap Thermal Delay (s)',
    description: 'Cool-down after catalog/bootstrap LLM calls.',
    type: 'number',
    step: '0.1',
  },
  {
    key: 'thermal_delay_cognitive',
    label: 'Cognitive Thermal Delay (s)',
    description: 'Cool-down after Phase-2 lint and enrichment inferences.',
    type: 'number',
    step: '0.1',
  },
  {
    key: 'mapreduce_trigger_chars',
    label: 'MapReduce Trigger (chars)',
    description: 'Giant pages above this size are chunked before LLM harvest.',
    type: 'number',
    step: '1000',
  },
  {
    key: 'mapreduce_chunk_chars',
    label: 'MapReduce Chunk (chars)',
    description: 'Maximum characters per MapReduce summarization chunk.',
    type: 'number',
    step: '1000',
  },
  {
    key: 'low_priority_mode',
    label: 'Hardware Guard (Low Priority)',
    description: 'POSIX niceness 19 — Plumber yields CPU to foreground work.',
    type: 'boolean',
  },
]

const TRUST_SECTIONS: TrustSection[] = [
  {
    id: 'safe',
    title: 'Safe Mode',
    subtitle: 'Reads context and adds metadata only. Recommended default.',
    borderClass: 'border-emerald-500/30',
    badgeClass: 'border-emerald-500/40 bg-emerald-950/40 text-emerald-300',
    fields: [
      {
        key: 'semantic_routing',
        label: 'Semantic Routing',
        description: 'Cache LLM index results by page fingerprint (read-only on disk).',
        type: 'boolean',
        badge: '🛡️ Safe',
        badgeClass: 'border-emerald-500/40 bg-emerald-950/40 text-emerald-300',
      },
      {
        key: 'context_compression',
        label: 'Context Compression',
        description: 'Rolling condensation of multi-turn LLM history in memory only.',
        type: 'boolean',
        badge: '🛡️ Safe',
        badgeClass: 'border-emerald-500/40 bg-emerald-950/40 text-emerald-300',
      },
      {
        key: 'entity_consolidation',
        label: 'Entity Consolidation',
        description: 'Adds alias:: lines on canonical pages when duplicates are detected.',
        type: 'boolean',
        badge: '📝 Metadata',
        badgeClass: 'border-emerald-500/40 bg-emerald-950/40 text-emerald-300',
      },
      {
        key: 'property_hygiene',
        label: 'Property Hygiene',
        description: 'Infers missing key:: value properties from page tags.',
        type: 'boolean',
        badge: '📝 Metadata',
        badgeClass: 'border-emerald-500/40 bg-emerald-950/40 text-emerald-300',
      },
      {
        key: 'marpa_framework',
        label: 'MARPA Framework',
        description: 'Assigns type:: domain metadata and validation side-sections.',
        type: 'boolean',
        badge: '📝 Metadata',
        badgeClass: 'border-emerald-500/40 bg-emerald-950/40 text-emerald-300',
      },
    ],
  },
  {
    id: 'augmented',
    title: 'Augmented Mode',
    subtitle: 'Adds content in isolated side-blocks or new pages — your bullets stay intact.',
    borderClass: 'border-amber-500/30',
    badgeClass: 'border-amber-500/40 bg-amber-950/40 text-amber-200',
    fields: [
      {
        key: 'heal_dangling',
        label: 'Heal Dangling Links',
        description: 'Creates isolated seed pages for broken [[WikiLinks]] — never edits your text.',
        type: 'boolean',
        badge: '📎 Side blocks',
        badgeClass: 'border-amber-500/40 bg-amber-950/40 text-amber-200',
      },
      {
        key: 'backpropagate_links',
        label: 'Backpropagate Links',
        description: 'Appends ### Matryca Backlink Context sections on target pages.',
        type: 'boolean',
        badge: '📎 Side blocks',
        badgeClass: 'border-amber-500/40 bg-amber-950/40 text-amber-200',
      },
    ],
  },
  {
    id: 'surgeon',
    title: 'Surgeon Mode',
    subtitle: 'Alters your original bullet text or block structure. Handle with care.',
    borderClass: 'border-rose-500/40',
    badgeClass: 'border-rose-500/40 bg-rose-950/40 text-rose-300',
    fields: [
      {
        key: 'enable_inline_semantic_corrections',
        label: 'Enable Inline Semantic Corrections',
        description:
          'Wraps concepts in [[WikiLinks]] inside your bullets. Stamps matryca-plumber:: true for audit.',
        type: 'boolean',
        badge: '⚠️ Modifies text',
        badgeClass: 'border-rose-500/40 bg-rose-950/40 text-rose-300',
      },
      {
        key: 'auto_split',
        label: 'Auto-Split Dense Blocks',
        description: 'Extracts oversized subtrees into new pages and replaces them with link stubs.',
        type: 'boolean',
        badge: '💣 Structural',
        badgeClass: 'border-rose-600/50 bg-rose-950/50 text-rose-200',
      },
    ],
  },
]

function emptyDraft(): PlumberConfig {
  return {
    logseq_graph_path: '',
    lm_studio_url: 'http://localhost:1234/v1',
    low_priority_mode: true,
    thermal_delay_bootstrap: 2,
    thermal_delay_cognitive: 2,
    mapreduce_trigger_chars: 25000,
    mapreduce_chunk_chars: 15000,
    context_compression: false,
    semantic_routing: false,
    entity_consolidation: false,
    property_hygiene: false,
    marpa_framework: false,
    heal_dangling: false,
    backpropagate_links: false,
    enable_inline_semantic_corrections: false,
    auto_split: false,
  }
}

function RiskBadge({ label, className }: { label: string; className: string }) {
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider ${className}`}
    >
      {label}
    </span>
  )
}

function ConfigField({
  field,
  draft,
  onChange,
}: {
  field: FieldSpec
  draft: PlumberConfig
  onChange: <K extends keyof PlumberConfig>(key: K, raw: PlumberConfig[K]) => void
}) {
  return (
    <label className="block space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs font-medium text-slate-200">{field.label}</span>
        {field.badge ? (
          <RiskBadge label={field.badge} className={field.badgeClass ?? 'border-slate-700 text-slate-400'} />
        ) : null}
      </div>
      <p className="text-[11px] leading-relaxed text-slate-500">{field.description}</p>
      {field.type === 'boolean' ? (
        <input
          type="checkbox"
          checked={Boolean(draft[field.key])}
          onChange={(event) =>
            onChange(field.key, event.target.checked as PlumberConfig[typeof field.key])
          }
          className="mt-1 h-4 w-4 rounded border-slate-700 bg-slate-900 accent-emerald-500"
        />
      ) : field.type === 'number' ? (
        <input
          type="number"
          step={field.step}
          value={Number(draft[field.key])}
          onChange={(event) =>
            onChange(field.key, Number(event.target.value) as PlumberConfig[typeof field.key])
          }
          className="w-full rounded-lg border border-slate-800 bg-slate-900/80 px-3 py-2 font-mono text-xs text-slate-100 outline-none ring-cyber-cyan/40 focus:ring-2"
        />
      ) : (
        <input
          type="text"
          value={String(draft[field.key])}
          onChange={(event) =>
            onChange(field.key, event.target.value as PlumberConfig[typeof field.key])
          }
          className="w-full rounded-lg border border-slate-800 bg-slate-900/80 px-3 py-2 font-mono text-xs text-slate-100 outline-none ring-cyber-cyan/40 focus:ring-2"
        />
      )}
    </label>
  )
}

function TrustSectionCard({
  section,
  draft,
  expanded,
  onToggle,
  onChange,
}: {
  section: TrustSection
  draft: PlumberConfig
  expanded: boolean
  onToggle: () => void
  onChange: <K extends keyof PlumberConfig>(key: K, raw: PlumberConfig[K]) => void
}) {
  const emoji = section.id === 'safe' ? '🟢' : section.id === 'augmented' ? '🟠' : '🔴'

  return (
    <section className={`rounded-xl border ${section.borderClass} bg-slate-950/40`}>
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-start justify-between gap-3 px-4 py-3 text-left"
      >
        <div>
          <p className="font-mono text-xs font-semibold text-slate-100">
            {emoji} {section.title}
          </p>
          <p className="mt-1 text-[11px] leading-relaxed text-slate-500">{section.subtitle}</p>
        </div>
        <span className="font-mono text-[10px] text-slate-600">{expanded ? '−' : '+'}</span>
      </button>
      {expanded ? (
        <div className="space-y-5 border-t border-slate-800/80 px-4 py-4">
          {section.fields.map((field) => (
            <ConfigField key={field.key} field={field} draft={draft} onChange={onChange} />
          ))}
        </div>
      ) : null}
    </section>
  )
}

export function SettingsDrawer({ open, config, onClose, onSave }: SettingsDrawerProps) {
  const [draft, setDraft] = useState<PlumberConfig>(emptyDraft())
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [expandedSections, setExpandedSections] = useState<Record<string, boolean>>({
    safe: true,
    augmented: true,
    surgeon: true,
  })

  useEffect(() => {
    if (config) {
      setDraft(config)
    }
  }, [config])

  useEffect(() => {
    if (!open) {
      setSaved(false)
    }
  }, [open])

  const updateField = <K extends keyof PlumberConfig>(key: K, raw: PlumberConfig[K]) => {
    setDraft((prev) => ({ ...prev, [key]: raw }))
    setSaved(false)
  }

  const handleSave = async () => {
    setSaving(true)
    const result = await onSave(draft)
    setSaving(false)
    if (result) {
      setSaved(true)
    }
  }

  const toggleSection = (id: string) => {
    setExpandedSections((prev) => ({ ...prev, [id]: !prev[id] }))
  }

  return (
    <>
      <div
        className={`fixed inset-0 z-40 bg-black/60 backdrop-blur-sm transition-opacity duration-300 ${
          open ? 'pointer-events-auto opacity-100' : 'pointer-events-none opacity-0'
        }`}
        onClick={onClose}
        aria-hidden={!open}
      />
      <aside
        className={`fixed top-0 left-0 z-50 h-full w-[28rem] max-w-[92vw] transform border-r border-slate-800 bg-slate-950 shadow-2xl transition-transform duration-300 ease-out ${
          open ? 'translate-x-0' : '-translate-x-full'
        }`}
        aria-hidden={!open}
        aria-label="Environment settings"
      >
        <div className="flex h-full flex-col">
          <header className="border-b border-slate-800 px-5 py-4">
            <p className="font-mono text-[10px] uppercase tracking-[0.35em] text-slate-500">
              Configuration
            </p>
            <h2 className="mt-1 font-mono text-sm font-semibold text-slate-100">Trust &amp; Safety</h2>
            <p className="mt-1 text-xs text-slate-500">
              Changes persist to <code className="text-cyber-cyan">.env</code> and hot-reload the daemon.
            </p>
          </header>

          <form
            className="flex-1 overflow-y-auto px-5 py-4 terminal-scroll"
            onSubmit={(event) => {
              event.preventDefault()
              void handleSave()
            }}
          >
            <div className="space-y-6">
              <div>
                <p className="mb-3 font-mono text-[10px] uppercase tracking-[0.3em] text-slate-600">
                  Infrastructure
                </p>
                <div className="space-y-5">
                  {INFRA_FIELDS.map((field) => (
                    <ConfigField key={field.key} field={field} draft={draft} onChange={updateField} />
                  ))}
                </div>
              </div>

              <div>
                <p className="mb-3 font-mono text-[10px] uppercase tracking-[0.3em] text-slate-600">
                  Invasiveness Matrix
                </p>
                <div className="space-y-3">
                  {TRUST_SECTIONS.map((section) => (
                    <TrustSectionCard
                      key={section.id}
                      section={section}
                      draft={draft}
                      expanded={expandedSections[section.id] ?? true}
                      onToggle={() => toggleSection(section.id)}
                      onChange={updateField}
                    />
                  ))}
                </div>
              </div>
            </div>
          </form>

          <footer className="border-t border-slate-800 px-5 py-4">
            <button
              type="button"
              disabled={saving}
              onClick={() => void handleSave()}
              className="w-full rounded-lg border border-emerald-500/50 bg-emerald-950/50 px-4 py-2.5 font-mono text-xs font-semibold uppercase tracking-wider text-emerald-300 shadow-[0_0_18px_rgb(52_211_153_/_0.35)] transition hover:bg-emerald-900/40 disabled:opacity-50"
            >
              {saving ? 'Saving…' : saved ? 'Saved & Applied ✓' : 'Save & Apply Changes'}
            </button>
          </footer>
        </div>
      </aside>
    </>
  )
}
