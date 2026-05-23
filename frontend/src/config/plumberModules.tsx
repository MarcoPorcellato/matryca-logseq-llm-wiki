import type { ReactNode } from 'react'

import type { PlumberConfig } from '../types/daemon'

export type PlumberModuleKey = Extract<
  {
    [K in keyof PlumberConfig]: PlumberConfig[K] extends boolean ? K : never
  }[keyof PlumberConfig],
  | 'low_priority_mode'
  | 'semantic_routing'
  | 'context_compression'
  | 'entity_consolidation'
  | 'property_hygiene'
  | 'marpa_framework'
  | 'heal_dangling'
  | 'backpropagate_links'
  | 'enable_inline_semantic_corrections'
  | 'auto_split'
>

export interface PlumberModuleSpec {
  id: string
  configKey: PlumberModuleKey
  label: string
  emoji: string
  icon: ReactNode
}

function IconNetwork() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.75">
      <circle cx="12" cy="12" r="2" />
      <circle cx="5" cy="7" r="2" />
      <circle cx="19" cy="7" r="2" />
      <circle cx="5" cy="17" r="2" />
      <circle cx="19" cy="17" r="2" />
      <path d="M7 8.5 10 10.5M14 10.5l3-2M7 15.5l3-2M14 13.5l3 2" />
    </svg>
  )
}

function IconZap() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.75">
      <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8Z" />
    </svg>
  )
}

function IconUsers() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.75">
      <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  )
}

function IconTags() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.75">
      <path d="M12 2 2 7l10 5 10-5-10-5Z" />
      <path d="m2 12 10 5 10-5" />
      <circle cx="7.5" cy="7.5" r="1.5" fill="currentColor" stroke="none" />
    </svg>
  )
}

function IconLayers() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.75">
      <path d="M12 2 2 7l10 5 10-5-10-5Z" />
      <path d="m2 12 10 5 10-5" />
      <path d="m2 17 10 5 10-5" />
    </svg>
  )
}

function IconLinkHeal() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.75">
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
    </svg>
  )
}

function IconGitBranch() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.75">
      <path d="M6 3v12" />
      <circle cx="6" cy="18" r="3" />
      <circle cx="6" cy="6" r="3" />
      <path d="M18 6a3 3 0 0 0-3 3v7" />
      <circle cx="18" cy="18" r="3" />
    </svg>
  )
}

function IconPenLine() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.75">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
    </svg>
  )
}

function IconSplit() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.75">
      <path d="M16 3h5v5" />
      <path d="M8 3H3v5" />
      <path d="M21 3 14 10" />
      <path d="M3 3l7 7" />
      <path d="M12 22v-8" />
      <path d="M8 14h8" />
    </svg>
  )
}

function IconShieldAlert() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.75">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z" />
      <path d="M12 8v4M12 16h.01" />
    </svg>
  )
}

/** Boolean Plumber modules — labels mirror ``SettingsDrawer.tsx`` field specs. */
export const PLUMBER_MODULE_SPECS: PlumberModuleSpec[] = [
  {
    id: 'hardware',
    configKey: 'low_priority_mode',
    label: 'Hardware Guard',
    emoji: '🛡️',
    icon: <IconShieldAlert />,
  },
  {
    id: 'semantic_routing',
    configKey: 'semantic_routing',
    label: 'Semantic Routing',
    emoji: '🧭',
    icon: <IconNetwork />,
  },
  {
    id: 'context_compression',
    configKey: 'context_compression',
    label: 'Context Compression',
    emoji: '🗜️',
    icon: <IconZap />,
  },
  {
    id: 'entity_consolidation',
    configKey: 'entity_consolidation',
    label: 'Entity Consolidation',
    emoji: '👥',
    icon: <IconUsers />,
  },
  {
    id: 'property_hygiene',
    configKey: 'property_hygiene',
    label: 'Property Hygiene',
    emoji: '🏷️',
    icon: <IconTags />,
  },
  {
    id: 'marpa_framework',
    configKey: 'marpa_framework',
    label: 'MARPA Framework',
    emoji: '📐',
    icon: <IconLayers />,
  },
  {
    id: 'heal_dangling',
    configKey: 'heal_dangling',
    label: 'Heal Dangling Links',
    emoji: '🔗',
    icon: <IconLinkHeal />,
  },
  {
    id: 'backpropagate_links',
    configKey: 'backpropagate_links',
    label: 'Backpropagate Links',
    emoji: '🔄',
    icon: <IconGitBranch />,
  },
  {
    id: 'inline_corrections',
    configKey: 'enable_inline_semantic_corrections',
    label: 'Inline Semantic Corrections',
    emoji: '✏️',
    icon: <IconPenLine />,
  },
  {
    id: 'auto_split',
    configKey: 'auto_split',
    label: 'Auto-Split Dense Blocks',
    emoji: '✂️',
    icon: <IconSplit />,
  },
]

export function isPlumberModuleEnabled(
  config: PlumberConfig | null,
  configKey: PlumberModuleKey,
): boolean {
  if (!config) return false
  return Boolean(config[configKey])
}
