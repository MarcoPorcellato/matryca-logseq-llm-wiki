import type { ReactNode } from 'react'

import type { PlumberConfig } from '../types/daemon'
import { isEngineActive } from '../types/daemon'
import type { DaemonStatusValue } from '../types/daemon'

interface FeatureStatusBarProps {
  daemonStatus: DaemonStatusValue | undefined
  config: PlumberConfig | null
  frozen: boolean
}

interface FeatureBadge {
  id: string
  label: string
  emoji: string
  enabled: boolean
  icon: ReactNode
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

function IconCompass() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.75">
      <circle cx="12" cy="12" r="10" />
      <path d="m16.24 7.76-2.12 6.36-6.36 2.12 2.12-6.36 6.36-2.12Z" />
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

function buildFeatures(config: PlumberConfig | null): FeatureBadge[] {
  return [
    {
      id: 'outliner',
      label: 'Outliner Parser',
      emoji: '🌲',
      enabled: true,
      icon: <IconLayers />,
    },
    {
      id: 'louvain',
      label: 'Louvain Clustering',
      emoji: '🕸️',
      enabled: true,
      icon: <IconNetwork />,
    },
    {
      id: 'compression',
      label: 'Context Compression',
      emoji: '🗜️',
      enabled: config?.context_compression ?? false,
      icon: <IconZap />,
    },
    {
      id: 'backprop',
      label: 'Link Backpropagation',
      emoji: '🔄',
      enabled: config?.backpropagate_links ?? false,
      icon: <IconGitBranch />,
    },
    {
      id: 'seeding',
      label: 'Contextual Seeding',
      emoji: '🌱',
      enabled: config?.heal_dangling ?? false,
      icon: <IconCompass />,
    },
    {
      id: 'hardware',
      label: 'Hardware Guard',
      emoji: '🛡️',
      enabled: config?.low_priority_mode ?? false,
      icon: <IconShieldAlert />,
    },
  ]
}

export function FeatureStatusBar({ daemonStatus, config, frozen }: FeatureStatusBarProps) {
  const engineLive = !frozen && isEngineActive(daemonStatus)
  const features = buildFeatures(config)

  return (
    <section
      className="col-span-full rounded-2xl bg-theme-surface/45 px-4 py-3 shadow-sm ring-1 ring-theme-border/25 dark:bg-theme-surface/20"
      aria-label="Pipeline capability status"
    >
      <div className="flex flex-wrap items-center gap-2 sm:gap-3">
        <span className="mr-1 text-[10px] font-medium uppercase tracking-[0.3em] text-theme-muted">
          Modules
        </span>
        {features.map((feature) => {
          const active = engineLive && feature.enabled
          return (
            <div
              key={feature.id}
              title={`${feature.label}${active ? ' — active' : ' — inactive'}`}
              className={`group inline-flex items-center gap-2 rounded-lg border px-2.5 py-1.5 transition-all duration-300 ${
                active
                  ? 'border-theme-accent/60 bg-theme-accent-bg/30 text-theme-accent ring-1 ring-theme-accent/30'
                  : 'border-theme-border/50 bg-theme-base/40 text-theme-muted opacity-40'
              }`}
            >
              <span className="text-xs" aria-hidden>
                {feature.emoji}
              </span>
              <span className={active ? 'text-theme-accent' : 'text-theme-muted'}>{feature.icon}</span>
              <span className="hidden text-[10px] font-medium uppercase tracking-wider sm:inline">
                {feature.label}
              </span>
            </div>
          )
        })}
        {frozen && (
          <span className="ml-auto text-[10px] font-medium uppercase tracking-wider text-theme-muted">
            UI Frozen — 0Hz telemetry
          </span>
        )}
      </div>
    </section>
  )
}
