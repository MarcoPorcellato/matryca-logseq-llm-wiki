import type { PlumberConfig } from '../types/daemon'
import { isEngineActive } from '../types/daemon'
import type { DaemonStatusValue } from '../types/daemon'
import { isPlumberModuleEnabled, PLUMBER_MODULE_SPECS } from '../config/plumberModules'

interface FeatureStatusBarProps {
  daemonStatus: DaemonStatusValue | undefined
  config: PlumberConfig | null
  frozen: boolean
}

export function FeatureStatusBar({ daemonStatus, config, frozen }: FeatureStatusBarProps) {
  const engineLive = !frozen && isEngineActive(daemonStatus)

  return (
    <section
      className="shrink-0 rounded-2xl bg-theme-surface/45 px-4 py-3 shadow-sm ring-1 ring-theme-border/25 dark:bg-theme-surface/20"
      aria-label="Pipeline capability status"
    >
      <span className="mb-2 block text-xs font-medium uppercase tracking-[0.25em] text-theme-muted">
        Modules
      </span>
      <div className="flex flex-wrap items-center gap-2 sm:gap-3">
        {PLUMBER_MODULE_SPECS.map((module) => {
          const configured = isPlumberModuleEnabled(config, module.configKey)
          const running = configured && engineLive
          return (
            <div
              key={module.id}
              title={`${module.label}${
                running
                  ? ' — enabled in .env, engine running'
                  : configured
                    ? ' — enabled in .env'
                    : ' — disabled in .env'
              }`}
              className={`group inline-flex items-center gap-2 rounded-lg border px-2.5 py-1.5 transition-all duration-300 ${
                configured
                  ? `border-theme-accent/60 bg-theme-accent-bg/30 text-theme-accent ${
                      running ? 'ring-1 ring-theme-accent/30' : 'opacity-80'
                    }`
                  : 'border-theme-border/50 bg-theme-base/40 text-theme-muted opacity-40'
              }`}
            >
              <span className="text-xs" aria-hidden>
                {module.emoji}
              </span>
              <span className={configured ? 'text-theme-accent' : 'text-theme-muted'}>{module.icon}</span>
              <span className="hidden text-[10px] font-medium uppercase tracking-wider lg:inline">
                {module.label}
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
