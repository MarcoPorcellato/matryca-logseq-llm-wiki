import { useEffect, useRef } from 'react'

interface LiveConsoleProps {
  logs: string[]
}

const STICK_TO_BOTTOM_THRESHOLD_PX = 48

export function LiveConsole({ logs }: LiveConsoleProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const stickToBottomRef = useRef(true)

  useEffect(() => {
    const el = scrollRef.current
    if (!el || !stickToBottomRef.current) return
    el.scrollTop = el.scrollHeight
  }, [logs])

  const handleScroll = () => {
    const el = scrollRef.current
    if (!el) return
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    stickToBottomRef.current = distanceFromBottom <= STICK_TO_BOTTOM_THRESHOLD_PX
  }

  return (
    <section className="col-span-full flex h-[min(480px,60vh)] flex-col overflow-hidden rounded-2xl bg-theme-surface/45 shadow-sm ring-1 ring-theme-border/25 dark:bg-theme-surface/20">
      <div className="flex items-center gap-2 border-b border-theme-border/50 px-4 py-3">
        <span className="h-2.5 w-2.5 rounded-full bg-red-500/80" />
        <span className="h-2.5 w-2.5 rounded-full bg-theme-accent/80" />
        <span className="h-2.5 w-2.5 rounded-full bg-emerald-500/80" />
        <p className="ml-2 text-xs font-medium uppercase tracking-[0.2em] text-theme-muted">
          Live Console
        </p>
      </div>

      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="markdown-theme terminal-scroll min-h-0 flex-1 space-y-1.5 overflow-y-auto bg-theme-base p-4 text-sm"
      >
        {logs.length === 0 ? (
          <p className="text-theme-muted">Waiting for operational logs from /api/logs…</p>
        ) : (
          logs.map((line, index) => (
            <div
              key={`${index}-${line.slice(0, 24)}`}
              className="rounded-lg border border-theme-border/45 bg-theme-surface/60 px-3 py-1.5 font-mono text-xs shadow-sm"
            >
              <span className="mr-2 select-none text-emerald-500">›</span>
              <span className="whitespace-pre-wrap break-all text-theme-text">{line}</span>
            </div>
          ))
        )}
      </div>
    </section>
  )
}
