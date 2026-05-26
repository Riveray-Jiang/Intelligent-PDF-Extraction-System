import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import confetti from 'canvas-confetti'
import ReactMarkdown from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import rehypeRaw from 'rehype-raw'
import remarkMath from 'remark-math'
import {
  Brain,
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  Download,
  FileText,
  Layers3,
  LoaderCircle,
  Play,
  RefreshCw,
  RotateCw,
  Upload,
  X,
} from 'lucide-react'
import { cn } from './lib/cn'
import type {
  FileHistoryPayload,
  FileVersionHistoryEntry,
  JobSnapshot,
  OutlineItem,
  PagePreview,
  RunMode,
  RunHistoryEntry,
  RunHistoryPayload,
  SelectionMode,
  SessionPayload,
  UploadResponse,
} from './types'

type ArtifactViewerState = {
  title: string
  subtitle?: string
  sourceJobId?: string | null
  runId?: string | null
  pageNumbers: number[]
  navigationPageNumbers?: number[]
  pageRunIds?: Record<number, string | null | undefined>
  initialPage?: number
  markdownHref?: string
  jsonHref?: string
  initialTab?: 'markdown' | 'json'
  allowRepair?: boolean
}

type ArtifactViewMode = 'markdown' | 'json'

type ViewerRepairPhase = 'starting' | 'repairing' | 'refreshing' | 'done' | 'error'

type ViewerRepairTask = {
  pageNumber: number
  phase: ViewerRepairPhase
  runId?: string | null
  message?: string
  startedAt: number
}

type ArtifactImageAgentMeta = {
  kind: string | null
  language: string | null
  altText: string | null
  interpretationMarkdown: string | null
}

type TopBarProps = {
  rightSlot?: ReactNode
  onHome?: () => void
}

type ImageSize = {
  width: number
  height: number
}

const CURRENT_HISTORY_KEY = '__current__'

function parsePageRange(input: string, pageCount: number): number[] {
  const values = new Set<number>()
  for (const rawToken of input.split(',')) {
    const token = rawToken.trim()
    if (!token) continue
    if (token.includes('-')) {
      const [left, right] = token.split('-', 2).map((part) => Number.parseInt(part.trim(), 10))
      if (!Number.isFinite(left) || !Number.isFinite(right) || left <= 0 || right <= 0) {
        throw new Error('Use positive page numbers only.')
      }
      const start = Math.min(left, right)
      const end = Math.max(left, right)
      for (let page = start; page <= end; page += 1) {
        if (page <= pageCount) values.add(page)
      }
      continue
    }
    const page = Number.parseInt(token, 10)
    if (!Number.isFinite(page) || page <= 0) {
      throw new Error('Use positive page numbers only.')
    }
    if (page <= pageCount) values.add(page)
  }
  return [...values].sort((a, b) => a - b)
}

function compressPages(pages: number[]): string {
  if (!pages.length) return ''
  const sorted = [...pages].sort((a, b) => a - b)
  const ranges: string[] = []
  let start = sorted[0]
  let prev = sorted[0]
  for (const current of sorted.slice(1)) {
    if (current === prev + 1) {
      prev = current
      continue
    }
    ranges.push(start === prev ? `${start}` : `${start}-${prev}`)
    start = prev = current
  }
  ranges.push(start === prev ? `${start}` : `${start}-${prev}`)
  return ranges.join(',')
}

function getOutlineRange(item: OutlineItem, outline: OutlineItem[], pageCount: number): number[] {
  const starts = outline.map((entry) => entry.page_index)
  const following = starts.filter((pageIndex) => pageIndex > item.page_index)
  const end = following.length ? following[0] : pageCount
  const pages: number[] = []
  for (let page = item.page_index + 1; page <= end; page += 1) {
    pages.push(page)
  }
  return pages
}

function useJobIdState() {
  const initial = new URLSearchParams(window.location.search).get('job')
  const [jobId, setJobId] = useState<string | null>(initial)

  function update(next: string | null) {
    const url = new URL(window.location.href)
    if (next) url.searchParams.set('job', next)
    else url.searchParams.delete('job')
    window.history.replaceState({}, '', url)
    setJobId(next)
  }

  return { jobId, setJobId: update }
}

const backendOrigin = (() => {
  const configured = import.meta.env.VITE_BACKEND_URL?.trim()
  if (configured) return configured
  if (
    typeof window !== 'undefined' &&
    (window.location.hostname === '127.0.0.1' || window.location.hostname === 'localhost') &&
    window.location.port === '5173'
  ) {
    return 'http://127.0.0.1:8892'
  }
  return window.location.origin
})()

const currentRepairEngineVersion = 'mineru2.5-pro-direct-v1'

function apiUrl(path: string) {
  return new URL(path, `${backendOrigin}/`).toString()
}

function resolveBackendHref(value?: string | null) {
  if (!value) return undefined
  if (/^[a-z]+:\/\//i.test(value)) return value
  return new URL(value, `${backendOrigin}/`).toString()
}

function formatElapsed(startedAt?: string | null) {
  if (!startedAt) return null
  const start = Date.parse(startedAt)
  if (Number.isNaN(start)) return null
  const totalSeconds = Math.max(0, Math.floor((Date.now() - start) / 1000))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes >= 60) {
    const hours = Math.floor(minutes / 60)
    const remMinutes = minutes % 60
    return `${hours}h ${remMinutes}m`
  }
  return `${minutes}m ${String(seconds).padStart(2, '0')}s`
}

function formatDurationLabel(durationSec?: number | null) {
  if (durationSec === null || durationSec === undefined || !Number.isFinite(durationSec)) return null
  const totalSeconds = Math.max(0, Math.round(durationSec))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes >= 60) {
    const hours = Math.floor(minutes / 60)
    const remMinutes = minutes % 60
    return `${hours}h ${remMinutes}m`
  }
  if (minutes <= 0) return `${seconds}s`
  return `${minutes}m ${String(seconds).padStart(2, '0')}s`
}

function durationBetweenSeconds(startedAt?: string | null, finishedAt?: string | null) {
  if (!startedAt || !finishedAt) return null
  const started = Date.parse(startedAt)
  const finished = Date.parse(finishedAt)
  if (Number.isNaN(started) || Number.isNaN(finished) || finished < started) return null
  return (finished - started) / 1000
}

function formatTimestampLabel(timestamp?: string | null) {
  if (!timestamp) return null
  const value = Date.parse(timestamp)
  if (Number.isNaN(value)) return null
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value))
}

function describeHistorySelection(entry: RunHistoryEntry) {
  if (entry.selection_mode === 'all') return 'All pages'
  if (entry.selection_mode === 'outline') return entry.selection ? `Sections ${entry.selection}` : 'Selected sections'
  if (entry.selection_mode === 'pagerange') {
    if (!entry.selection) return 'Selected pages'
    return entry.selection.includes(',') || entry.selection.includes('-') ? `Pages ${entry.selection}` : `Page ${entry.selection}`
  }
  return entry.selection || 'Custom slice'
}

function compactHistorySelection(entry: RunHistoryEntry) {
  const full = describeHistorySelection(entry)
  if (full.length <= 28) return full

  if ((entry.selection_mode === 'outline' || entry.selection_mode === 'pagerange') && entry.selection) {
    const prefix = entry.selection_mode === 'outline' ? 'Sections' : 'Pages'
    const parts = entry.selection
      .split(',')
      .map((value) => value.trim())
      .filter(Boolean)
    if (parts.length > 0) {
      const visible = parts.slice(0, 4).join(',')
      return `${prefix} ${visible}${parts.length > 4 ? ',…' : '…'}`
    }
  }

  return `${full.slice(0, 27)}…`
}

function runHistoryHasArtifacts(entry: RunHistoryEntry) {
  return Boolean(
    entry.artifact_urls['document.md'] ||
      entry.artifact_urls.document_md ||
      entry.artifact_urls['document_ir.json'] ||
      entry.artifact_urls.document_ir_json,
  )
}

function describeRunSelection(selectionMode: string | null | undefined, selection: string | null | undefined, pageCount?: number) {
  if (selectionMode === 'outline') return selection ? `Sections ${selection}` : 'Selected sections'
  if (selectionMode === 'pagerange') {
    if (!selection) return 'Selected pages'
    return selection.includes(',') || selection.includes('-') ? `Pages ${selection}` : `Page ${selection}`
  }
  if (selectionMode === 'all') return pageCount ? `All ${pageCount} pages` : 'All pages'
  return selection || 'Custom slice'
}

function getRunModeLabel(mode: RunMode | null | undefined) {
  return mode === 'reliable' ? 'Repair' : 'Run'
}

function readArtifactImageAgentMeta(preview: PagePreview | null | undefined): ArtifactImageAgentMeta | null {
  const kind = preview?.image_agent_kind?.trim() || null
  const language = preview?.image_agent_language?.trim().toLowerCase() || null
  const altText = preview?.image_alt_text?.trim() || null
  const interpretationMarkdown = preview?.image_interpretation_markdown?.trim() || null
  if (!altText && !interpretationMarkdown) return null

  return {
    kind,
    language,
    altText,
    interpretationMarkdown,
  }
}

function formatPageCountLabel(count: number) {
  return `${count} page${count === 1 ? '' : 's'}`
}

function clampProgress(value: number) {
  return Math.max(0, Math.min(100, value))
}

function getProgressSoftCeiling(rawProgress: number) {
  const progress = clampProgress(rawProgress)
  if (progress >= 100) return 100
  if (progress >= 96) return 99
  if (progress >= 92) return 97
  if (progress >= 82) return 92
  if (progress >= 68) return 81
  if (progress >= 38) return 66
  if (progress >= 12) return 34
  return Math.min(progress + 10, 18)
}

function arePageListsEqual(left: number[], right: number[]) {
  if (left.length !== right.length) return false
  return left.every((page, index) => page === right[index])
}

async function readResponseErrorMessage(response: Response, fallback: string) {
  let raw = ''
  try {
    raw = (await response.text()).trim()
  } catch {
    return fallback
  }

  if (!raw) return fallback

  const looksLikeHtml =
    raw.startsWith('<!DOCTYPE') ||
    raw.startsWith('<html') ||
    (response.headers.get('content-type') ?? '').toLowerCase().includes('text/html')

  if (looksLikeHtml) {
    if (response.status === 404) {
      return `${fallback} Backend route returned 404. Refresh the page and retry.`
    }
    const stripped = raw.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
    return stripped || fallback
  }

  return raw
}

function compactViewerSubtitle(value?: string | null) {
  if (!value) return undefined
  const text = value.trim()
  return /^Page \d+$/i.test(text) ? undefined : text
}

function resolveSelectionPages(
  selectionMode: string | null | undefined,
  selection: string | null | undefined,
  pageCount: number,
  outline: OutlineItem[],
): number[] {
  const allPages = Array.from({ length: pageCount }, (_, index) => index + 1)
  if (!pageCount) return []

  if (!selectionMode || selectionMode === 'all') return allPages

  if (selectionMode === 'pagerange') {
    try {
      const pages = parsePageRange(selection ?? '', pageCount)
      return pages.length ? pages : allPages
    } catch {
      return allPages
    }
  }

  if (selectionMode === 'outline') {
    const ids = (selection ?? '')
      .split(',')
      .map((value) => Number.parseInt(value.trim(), 10))
      .filter((value) => Number.isFinite(value))
    if (!ids.length) return allPages
    const selected = outline.filter((item) => ids.includes(item.id))
    const pages = [...new Set(selected.flatMap((item) => getOutlineRange(item, outline, pageCount)))].sort((a, b) => a - b)
    return pages.length ? pages : allPages
  }

  return allPages
}

function getHistoryEntryPages(entry: RunHistoryEntry, pageCount: number, outline: OutlineItem[]) {
  if (entry.resolved_pages?.length) return entry.resolved_pages
  return resolveSelectionPages(entry.selection_mode, entry.selection, pageCount, outline)
}

function selectionBootstrap(payload: SessionPayload) {
  const pages = payload.pages.map((page) => page.page_index + 1)
  return {
    mode: 'all' as SelectionMode,
    selectedPages: pages,
    selectedOutlineIds: [] as number[],
    pageInput: compressPages(pages),
  }
}

function inferInitialWorkflowStep(session: SessionPayload | null | undefined, job: JobSnapshot | null | undefined) {
  if (!session) return 1
  if (job?.status === 'running') return 3
  if (job?.status === 'completed' || job?.status === 'failed' || job?.status === 'canceled') return 4
  return 2
}

type WorkflowStepState = 'done' | 'active' | 'pending'

type WorkflowSectionProps = {
  step: string
  title: string
  detail: string
  state: WorkflowStepState
  open: boolean
  badge?: ReactNode
  children?: ReactNode
  onToggle?: () => void
}

function WorkflowSection({ step, title, detail, state, open, badge, children, onToggle }: WorkflowSectionProps) {
  const highlighted = open

  return (
    <section
      className={cn(
        'relative overflow-hidden rounded-[24px] border bg-white/88 px-4 py-4 shadow-[0_10px_24px_rgba(15,23,42,0.04)] backdrop-blur-sm transition-all',
        highlighted
          ? 'border-[color:var(--theme-secondary)]/62 bg-[linear-gradient(165deg,rgba(244,252,215,0.98),rgba(227,244,228,0.98)_58%,rgba(242,249,242,0.98))] shadow-[0_22px_44px_rgba(124,145,36,0.16)] ring-1 ring-[color:var(--theme-secondary)]/36 before:absolute before:bottom-0 before:left-0 before:top-0 before:w-[3px] before:bg-[linear-gradient(180deg,#0b3b34,#b2cb35)]'
          : state === 'done'
            ? 'border-slate-300/80 bg-[rgba(248,251,246,0.94)]'
            : 'border-slate-300/80 bg-[rgba(255,255,255,0.78)]',
      )}
    >
      <button
        type="button"
        onClick={onToggle}
        className={cn('flex w-full items-start gap-3 text-left', !onToggle && 'cursor-default')}
      >
        <div className="flex w-full items-start gap-3">
          <div
            className={cn(
              'inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full border text-[11px] font-bold transition',
              state === 'done'
                ? 'border-[color:var(--theme-primary)]/20 bg-[color:var(--theme-primary)]/10 text-[color:var(--theme-primary)]'
                : highlighted
                  ? 'border-[color:var(--theme-secondary)]/70 bg-[linear-gradient(180deg,rgba(202,223,102,0.68),rgba(239,247,198,0.92))] text-[color:var(--theme-secondary-strong)] shadow-[0_12px_24px_rgba(178,203,53,0.22)]'
                  : 'border-slate-200 bg-white text-slate-500',
            )}
          >
            {state === 'done' ? <Check className="h-4 w-4" /> : step}
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <div className={cn('text-sm font-semibold text-slate-950', highlighted && 'text-[color:var(--theme-primary-strong)]')}>{title}</div>
                </div>
                <div className={cn('mt-1 text-sm leading-6', highlighted ? 'text-slate-700' : 'text-slate-500')}>{detail}</div>
              </div>
              <div className="flex items-center gap-2">
                {badge}
                {onToggle ? <ChevronRight className={cn('mt-0.5 h-4 w-4 shrink-0 text-slate-400 transition', open && 'rotate-90')} /> : null}
              </div>
            </div>
          </div>
        </div>
      </button>
      {open && children ? <div className="mt-4 border-t border-slate-300/70 pt-4">{children}</div> : null}
    </section>
  )
}

type ModeCardProps = {
  label: string
  hint: string
  checked: boolean
  disabled?: boolean
  onSelect: () => void
}

function ModeCard({ label, hint, checked, disabled, onSelect }: ModeCardProps) {
  return (
    <button
      type="button"
      onClick={onSelect}
      disabled={disabled}
      className={cn(
        'rounded-2xl border px-4 py-3.5 text-left transition-all',
        checked
          ? 'border-[color:var(--theme-primary)]/42 bg-[linear-gradient(180deg,rgba(0,77,64,0.08),rgba(178,203,53,0.12))] shadow-[0_18px_40px_rgba(0,77,64,0.1)] ring-1 ring-[color:var(--theme-primary)]/10'
          : 'border-slate-200 bg-white/80 hover:border-slate-300 hover:bg-white',
        disabled && 'cursor-not-allowed opacity-40',
      )}
    >
      <div className="flex items-start gap-3">
        <div
          className={cn(
            'mt-1 h-3 w-3 shrink-0 rounded-full border-2 transition-all',
            checked ? 'border-[color:var(--theme-primary)] bg-[color:var(--theme-primary)] shadow-[0_0_0_4px_rgba(0,77,64,0.08)]' : 'border-slate-300 bg-transparent',
          )}
        />
        <div>
          <div className={cn('text-sm font-semibold text-slate-900', checked && 'text-[color:var(--theme-primary-strong)]')}>{label}</div>
          <div className={cn('mt-1 text-xs leading-5 text-slate-500', checked && 'text-slate-700')}>{hint}</div>
        </div>
      </div>
    </button>
  )
}

type StatusBadgeProps = {
  status: JobSnapshot['status'] | 'idle'
}

function StatusBadge({ status }: StatusBadgeProps) {
  const tone =
    status === 'running'
      ? 'bg-[color:var(--theme-secondary)]/25 text-[color:var(--theme-secondary-strong)] ring-[color:var(--theme-secondary)]/50'
      : status === 'canceled'
          ? 'bg-slate-200 text-slate-700 ring-slate-300'
        : status === 'failed'
          ? 'bg-rose-100 text-rose-700 ring-rose-200'
        : status === 'ready'
            ? 'bg-[color:var(--theme-primary)]/10 text-[color:var(--theme-primary)] ring-[color:var(--theme-primary)]/18'
            : 'bg-slate-100 text-slate-500 ring-slate-200'
  return <span className={cn('inline-flex rounded-full px-3 py-1 text-[11px] font-bold uppercase tracking-[0.18em] ring-1', tone)}>{status}</span>
}

function Brand({ onHome }: { onHome?: () => void }) {
  return (
    <button
      type="button"
      onClick={onHome}
      className="inline-flex items-center gap-3.5 rounded-full text-left transition hover:opacity-90 focus:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--theme-primary)]/25"
      aria-label="Go to home"
    >
      <span className="relative inline-flex h-11 w-11 items-center justify-center rounded-[16px] border border-white/80 bg-[linear-gradient(180deg,#ffffff_0%,#f2f6f1_100%)] shadow-[0_14px_34px_rgba(0,77,64,0.08)] ring-1 ring-[color:var(--theme-border)]/65">
        <span className="absolute inset-[11px] translate-x-[3px] translate-y-[3px] rounded-[8px] border border-[color:var(--theme-border)] bg-[color:var(--theme-surface-soft)]" />
        <span className="absolute inset-[10px] rounded-[8px] border border-[color:var(--theme-border-strong)] bg-white" />
        <span className="absolute left-[14px] top-[15px] h-[2px] w-5 rounded-full bg-[color:var(--theme-primary)]" />
        <span className="absolute left-[14px] top-[21px] h-[2px] w-3.5 rounded-full bg-[color:var(--theme-secondary)]" />
        <span className="absolute left-[14px] top-[27px] h-[2px] w-4.5 rounded-full bg-[#91a59b]" />
      </span>
      <span className="flex flex-col leading-none">
        <span className="block text-[20px] font-semibold tracking-[-0.035em] text-slate-950">PDF Extraction</span>
      </span>
    </button>
  )
}

function ImageAgentMark({ compact = false }: { compact?: boolean }) {
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center whitespace-nowrap rounded-full border border-[color:var(--theme-primary)]/14 bg-[linear-gradient(180deg,rgba(0,77,64,0.08),rgba(178,203,53,0.1))] text-[color:var(--theme-primary-strong)] shadow-[0_12px_28px_rgba(0,77,64,0.08)]',
        compact ? 'px-1.5 py-1' : 'gap-2 px-2.5 py-1.5',
      )}
    >
      <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-[linear-gradient(180deg,#eef7d0,#dce9a0)] text-[color:var(--theme-primary-strong)] shadow-[inset_0_1px_0_rgba(255,255,255,0.85),0_6px_16px_rgba(0,77,64,0.12)]">
        <Brain className="h-3 w-3" strokeWidth={2.2} />
      </span>
      {!compact && <span className="text-[11px] font-semibold tracking-[-0.01em]">Image Agent</span>}
    </span>
  )
}

function TopBar({ rightSlot, onHome }: TopBarProps) {
  return (
    <header className="flex items-center justify-between gap-4">
      <Brand onHome={onHome} />
      {rightSlot ?? <div />}
    </header>
  )
}

function launchCompletionCelebration() {
  const palette = ['#ffd166', '#ff8a5b', '#f472b6', '#60a5fa', '#c084fc', '#b2cb35']
  const timers: number[] = []
  const fire = (particleCount: number, options: Parameters<typeof confetti>[0]) => {
    void confetti({
      particleCount,
      zIndex: 2000,
      disableForReducedMotion: false,
      colors: palette,
      ...options,
    })
  }

  fire(180, {
    spread: 96,
    startVelocity: 48,
    ticks: 300,
    scalar: 1.16,
    gravity: 0.94,
    origin: { x: 0.5, y: 0.16 },
  })

  timers.push(
    window.setTimeout(() => {
      fire(110, {
        spread: 74,
        startVelocity: 52,
        ticks: 270,
        scalar: 1.02,
        origin: { x: 0.22, y: 0.24 },
      })
    }, 160),
  )

  timers.push(
    window.setTimeout(() => {
      fire(110, {
        spread: 74,
        startVelocity: 52,
        ticks: 270,
        scalar: 1.02,
        origin: { x: 0.78, y: 0.24 },
      })
    }, 220),
  )

  timers.push(
    window.setTimeout(() => {
      fire(72, {
        spread: 118,
        startVelocity: 36,
        ticks: 340,
        scalar: 1.2,
        gravity: 0.88,
        origin: { x: 0.5, y: 0.12 },
      })
    }, 420),
  )

  timers.push(
    window.setTimeout(() => {
      fire(90, {
        angle: 58,
        spread: 72,
        startVelocity: 54,
        ticks: 300,
        scalar: 1,
        origin: { x: 0.02, y: 0.78 },
      })
    }, 120),
  )

  timers.push(
    window.setTimeout(() => {
      fire(90, {
        angle: 122,
        spread: 72,
        startVelocity: 54,
        ticks: 300,
        scalar: 1,
        origin: { x: 0.98, y: 0.78 },
      })
    }, 150),
  )

  timers.push(
    window.setTimeout(() => {
      fire(64, {
        spread: 128,
        startVelocity: 26,
        ticks: 380,
        scalar: 1.28,
        gravity: 0.82,
        drift: 0.12,
        origin: { x: 0.5, y: 0.1 },
      })
    }, 760),
  )

  return () => {
    timers.forEach((timer) => window.clearTimeout(timer))
  }
}

function wait(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

export default function App() {
  const { jobId, setJobId } = useJobIdState()
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const previousStatusRef = useRef<JobSnapshot['status'] | 'idle'>('idle')
  const lastCelebratedSignatureRef = useRef<string | null>(null)
  const artifactPreviewRequestRef = useRef(0)
  const lastRepairRefreshRunIdRef = useRef<string | null>(null)

  const [session, setSession] = useState<SessionPayload | null>(null)
  const [job, setJob] = useState<JobSnapshot | null>(null)
  const [loadingSession, setLoadingSession] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [uploadIntent, setUploadIntent] = useState<'new' | 'replace'>('new')

  const [selectionMode, setSelectionMode] = useState<SelectionMode>('all')
  const [selectedPages, setSelectedPages] = useState<number[]>([])
  const [selectedOutlineIds, setSelectedOutlineIds] = useState<number[]>([])
  const [pageInput, setPageInput] = useState('')
  const [pageInputError, setPageInputError] = useState<string | null>(null)
  const [outputDir, setOutputDir] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [runHistory, setRunHistory] = useState<RunHistoryEntry[]>([])
  const [fileHistory, setFileHistory] = useState<FileHistoryPayload | null>(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyRefreshKey, setHistoryRefreshKey] = useState(0)

  const [artifactViewer, setArtifactViewer] = useState<ArtifactViewerState | null>(null)
  const [artifactPage, setArtifactPage] = useState<number | null>(null)
  const [artifactPreviewData, setArtifactPreviewData] = useState<PagePreview | null>(null)
  const [artifactPreviewError, setArtifactPreviewError] = useState<string | null>(null)
  const [artifactLoading, setArtifactLoading] = useState(false)
  const [repairActionError, setRepairActionError] = useState<string | null>(null)
  const [artifactRepairingPage, setArtifactRepairingPage] = useState<number | null>(null)
  const [viewerRepairTask, setViewerRepairTask] = useState<ViewerRepairTask | null>(null)
  const [artifactPageRunOverrides, setArtifactPageRunOverrides] = useState<Record<number, string | null | undefined>>({})
  const [artifactViewMode, setArtifactViewMode] = useState<ArtifactViewMode>('markdown')
  const [artifactImageRotation, setArtifactImageRotation] = useState(0)
  const [artifactImageNaturalSize, setArtifactImageNaturalSize] = useState<ImageSize | null>(null)
  const [artifactImageViewportSize, setArtifactImageViewportSize] = useState<ImageSize | null>(null)
  const [artifactImageAgentLoading, setArtifactImageAgentLoading] = useState(false)
  const [artifactImageAgentError, setArtifactImageAgentError] = useState<string | null>(null)
  const [artifactImageAgentPanelOpen, setArtifactImageAgentPanelOpen] = useState(false)
  const [copiedSurface, setCopiedSurface] = useState<string | null>(null)
  const [cancelingRun, setCancelingRun] = useState(false)
  const [, setElapsedTick] = useState(0)
  const [displayedRunProgress, setDisplayedRunProgress] = useState(0)
  const [expandedWorkflowStep, setExpandedWorkflowStep] = useState(1)
  const [expandedHistoryVersions, setExpandedHistoryVersions] = useState<string[]>([])
  const artifactImageViewportRef = useRef<HTMLDivElement | null>(null)
  const artifactThumbStripRef = useRef<HTMLDivElement | null>(null)

  const jobStatusForHistory = job?.status
  const jobFinishedAtForHistory = job?.finished_at

  useEffect(() => {
    if (!jobId) {
      setSession(null)
      setJob(null)
      setRunHistory([])
      setFileHistory(null)
      setExpandedHistoryVersions([])
      setHistoryRefreshKey(0)
      setLoadingSession(false)
      lastCelebratedSignatureRef.current = null
      return
    }

    let active = true
    async function loadSession() {
      try {
        setLoadingSession(true)
        setLoadError(null)
        const response = await fetch(apiUrl(`/api/jobs/${jobId}/session`), { cache: 'no-store' })
        if (!response.ok) {
          throw new Error(`Failed to load job ${jobId}.`)
        }
      const payload = (await response.json()) as SessionPayload
      if (!active) return
      const initialCompletionSignature =
        payload.job?.finished_at && payload.job?.output_dir ? `${payload.job.finished_at}:${payload.job.output_dir}` : null
      lastCelebratedSignatureRef.current = initialCompletionSignature
      setSession(payload)
      setJob(payload.job)
        const bootstrap = selectionBootstrap(payload)
        setSelectionMode(bootstrap.mode)
        setSelectedPages(bootstrap.selectedPages)
        setSelectedOutlineIds(bootstrap.selectedOutlineIds)
        setPageInput(bootstrap.pageInput)
        setOutputDir(payload.default_output_dir)
        setExpandedWorkflowStep(inferInitialWorkflowStep(payload, payload.job))
      } catch (caught) {
        if (!active) return
        setLoadError(caught instanceof Error ? caught.message : 'Failed to load document.')
      } finally {
        if (active) setLoadingSession(false)
      }
    }

    void loadSession()
    return () => {
      active = false
    }
  }, [jobId])

  useEffect(() => {
    if (!jobId) return
    let active = true

    async function poll() {
      try {
        const response = await fetch(apiUrl(`/api/jobs/${jobId}/status`), { cache: 'no-store' })
        if (!response.ok) return
        const payload = (await response.json()) as JobSnapshot
        if (active) setJob(payload)
      } catch {
        // ignore transient polling failures
      }
    }

    void poll()
    const timer = window.setInterval(() => {
      void poll()
    }, 1800)

    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [jobId])

  useEffect(() => {
    if (!jobId) {
      setRunHistory([])
      setFileHistory(null)
      setHistoryLoading(false)
      return
    }

    let active = true
    async function loadHistory() {
      try {
        setHistoryLoading(true)
        const [runsResult, fileHistoryResult] = await Promise.allSettled([
          fetch(apiUrl(`/api/jobs/${jobId}/runs`), { cache: 'no-store' }),
          fetch(apiUrl(`/api/jobs/${jobId}/file-history`), { cache: 'no-store' }),
        ])
        if (!active) return

        if (runsResult.status === 'fulfilled' && runsResult.value.ok) {
          const payload = (await runsResult.value.json()) as RunHistoryPayload
          setRunHistory(payload.runs ?? [])
        } else {
          setRunHistory([])
        }

        if (fileHistoryResult.status === 'fulfilled' && fileHistoryResult.value.ok) {
          const payload = (await fileHistoryResult.value.json()) as FileHistoryPayload
          setFileHistory(payload)
        } else {
          setFileHistory(null)
        }
      } catch {
        if (active) {
          setRunHistory([])
          setFileHistory(null)
        }
      } finally {
        if (active) setHistoryLoading(false)
      }
    }

    void loadHistory()
    return () => {
      active = false
    }
  }, [historyRefreshKey, jobFinishedAtForHistory, jobId, jobStatusForHistory])

  useEffect(() => {
    if (job?.status !== 'running' || !job.started_at) return
    const timer = window.setInterval(() => {
      setElapsedTick((value) => value + 1)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [job?.status, job?.started_at])

  const runningSignature = `${job?.run_id ?? ''}:${job?.started_at ?? ''}`
  const runProgressStatus = job?.status
  const runProgressPercent = job?.progress_percent

  useEffect(() => {
    if (!runProgressStatus) {
      setDisplayedRunProgress(0)
      return
    }
    if (runProgressStatus !== 'running') {
      setDisplayedRunProgress(clampProgress(runProgressPercent ?? (runProgressStatus === 'ready' ? 100 : 0)))
      return
    }
    setDisplayedRunProgress(clampProgress(runProgressPercent ?? 0))
  }, [runProgressPercent, runningSignature, runProgressStatus])

  useEffect(() => {
    if (job?.status !== 'running') return
    const rawProgress = clampProgress(job.progress_percent ?? 0)
    const softCeiling = getProgressSoftCeiling(rawProgress)
    const timer = window.setInterval(() => {
      setDisplayedRunProgress((current) => {
        const normalized = clampProgress(current)
        if (rawProgress > normalized) {
          const delta = rawProgress - normalized
          return clampProgress(normalized + Math.max(1.2, delta * 0.32))
        }
        if (normalized >= softCeiling) {
          return normalized
        }
        const remaining = softCeiling - normalized
        return clampProgress(normalized + Math.max(0.2, remaining * 0.055))
      })
    }, 700)
    return () => window.clearInterval(timer)
  }, [job?.status, job?.progress_percent, runningSignature])

  useEffect(() => {
    if (!artifactViewer) {
      setArtifactPage(null)
      setArtifactPreviewData(null)
      setArtifactPreviewError(null)
      setArtifactLoading(false)
      setRepairActionError(null)
      setArtifactRepairingPage(null)
      setViewerRepairTask(null)
      setArtifactPageRunOverrides({})
      setArtifactImageRotation(0)
      setArtifactImageNaturalSize(null)
      setArtifactImageViewportSize(null)
      setArtifactImageAgentLoading(false)
      setArtifactImageAgentError(null)
      setArtifactImageAgentPanelOpen(false)
      return
    }
    setRepairActionError(null)
    setArtifactRepairingPage(null)
    setViewerRepairTask(null)
    setArtifactPageRunOverrides({})
    setArtifactImageRotation(0)
    setArtifactImageNaturalSize(null)
    setArtifactImageAgentLoading(false)
    setArtifactImageAgentError(null)
    setArtifactImageAgentPanelOpen(false)
    lastRepairRefreshRunIdRef.current = null
    setArtifactViewMode(artifactViewer.initialTab ?? (artifactViewer.markdownHref ? 'markdown' : 'json'))
    setArtifactPage(artifactViewer.initialPage ?? artifactViewer.pageNumbers[0] ?? 1)
  }, [artifactViewer])

  useEffect(() => {
    const task = viewerRepairTask
    if (!task || task.phase === 'refreshing' || task.phase === 'done' || task.phase === 'error') {
      return
    }
    const jobIsRepair = job?.run_mode === 'reliable'
    const observedRunId = task.runId ?? (jobIsRepair ? job?.run_id ?? null : null)
    if (task.runId && job?.run_id && job.run_id !== task.runId) return
    if (!task.runId && !jobIsRepair) return

    if (!task.runId && observedRunId) {
      setViewerRepairTask((current) =>
        current?.startedAt === task.startedAt
          ? { ...current, phase: 'repairing', runId: observedRunId, message: 'Repair is running' }
          : current,
      )
    }

    if (job?.status === 'completed' && observedRunId) {
      lastRepairRefreshRunIdRef.current = observedRunId
      setViewerRepairTask((current) =>
        current?.startedAt === task.startedAt
          ? { ...current, phase: 'refreshing', runId: observedRunId, message: 'Refreshing output' }
          : current,
      )
      setArtifactPreviewData(null)
      setArtifactPreviewError(null)
      setArtifactImageAgentError(null)
      setArtifactImageAgentPanelOpen(false)
      setArtifactLoading(true)
      setHistoryRefreshKey((value) => value + 1)
      return
    }

    if (job?.status === 'failed' || job?.status === 'canceled') {
      setArtifactRepairingPage(null)
      setViewerRepairTask((current) =>
        current?.startedAt === task.startedAt
          ? {
              ...current,
              phase: 'error',
              message: job.message || 'Repair failed. Please try again.',
            }
          : current,
      )
    }
  }, [job?.message, job?.run_id, job?.run_mode, job?.status, viewerRepairTask])

  useEffect(() => {
    const task = viewerRepairTask
    if (!task || task.phase !== 'refreshing') return
    if (!jobId || !task.runId) return

    let active = true
    const pageNumber = task.pageNumber
    const runId = task.runId
    const startedAt = task.startedAt

    async function refreshRepairedPreview() {
      try {
        setArtifactLoading(true)
        setArtifactPreviewError(null)

        let payload: PagePreview | null = null
        for (let attempt = 0; attempt < 12; attempt += 1) {
          const url = new URL(apiUrl(`/api/jobs/${jobId}/page-preview`))
          url.searchParams.set('page', String(pageNumber))
          url.searchParams.set('run_id', runId)
          const response = await fetch(url.toString(), { cache: 'no-store' })
          if (!response.ok) {
            throw new Error(`Preview request failed: ${response.status}`)
          }
          payload = (await response.json()) as PagePreview
          if (!active) return
          if (payload.run_id !== runId) {
            throw new Error('Output did not refresh. Try again.')
          }
          if (payload.in_document_ir) {
            break
          }
          if (attempt < 11) {
            await wait(750)
          }
        }

        if (!payload?.in_document_ir) {
          throw new Error('Output is still preparing. Try again.')
        }
        setArtifactPageRunOverrides((current) => ({ ...current, [pageNumber]: runId }))
        if (artifactPage === pageNumber) {
          setArtifactPreviewData(payload)
        }
        setArtifactRepairingPage(null)
        setViewerRepairTask((current) =>
          current?.startedAt === startedAt ? { ...current, phase: 'done', message: 'Updated' } : current,
        )
        launchCompletionCelebration()
      } catch (caught) {
        if (!active) return
        const message = caught instanceof Error ? caught.message : 'Failed to refresh output.'
        setArtifactRepairingPage(null)
        setArtifactPreviewError(message)
        setViewerRepairTask((current) =>
          current?.startedAt === startedAt
            ? {
                ...current,
                phase: 'error',
                message,
              }
            : current,
        )
      } finally {
        if (active) setArtifactLoading(false)
      }
    }

    void refreshRepairedPreview()
    return () => {
      active = false
    }
  }, [artifactPage, jobId, viewerRepairTask])

  useEffect(() => {
    if (viewerRepairTask?.phase !== 'done') return
    const startedAt = viewerRepairTask.startedAt
    const timer = window.setTimeout(() => {
      setViewerRepairTask((current) => (current?.startedAt === startedAt ? null : current))
    }, 1600)
    return () => window.clearTimeout(timer)
  }, [viewerRepairTask?.phase, viewerRepairTask?.startedAt])

  useEffect(() => {
    const node = artifactImageViewportRef.current
    if (!node) {
      setArtifactImageViewportSize(null)
      return
    }

    const updateViewportSize = () => {
      setArtifactImageViewportSize({
        width: node.clientWidth,
        height: node.clientHeight,
      })
    }

    updateViewportSize()
    const observer = new ResizeObserver(updateViewportSize)
    observer.observe(node)
    return () => observer.disconnect()
  }, [artifactViewer, artifactPage, artifactImageRotation])

  useEffect(() => {
    const previewJobId = artifactViewer?.sourceJobId ?? jobId
    if (!previewJobId || !artifactViewer || artifactPage === null) {
      setArtifactPreviewData(null)
      setArtifactPreviewError(null)
      return
    }
    const viewer = artifactViewer
    let active = true
    const requestId = artifactPreviewRequestRef.current + 1
    artifactPreviewRequestRef.current = requestId

    async function loadArtifactPreview() {
      try {
        setArtifactLoading(true)
        setArtifactPreviewError(null)
        const url = new URL(apiUrl(`/api/jobs/${previewJobId}/page-preview`))
        url.searchParams.set('page', String(artifactPage))
        const pageRunId =
          artifactPage === null
            ? viewer.runId
            : artifactPageRunOverrides[artifactPage] ?? viewer.pageRunIds?.[artifactPage] ?? viewer.runId
        if (pageRunId) url.searchParams.set('run_id', pageRunId)
        const response = await fetch(url.toString(), { cache: 'no-store' })
        if (!response.ok) throw new Error(`Preview request failed: ${response.status}`)
        const payload = (await response.json()) as PagePreview
        if (!active || artifactPreviewRequestRef.current !== requestId) return
        setArtifactPreviewData(payload)
      } catch (caught) {
        if (!active || artifactPreviewRequestRef.current !== requestId) return
        setArtifactPreviewError(caught instanceof Error ? caught.message : 'Failed to load parse preview.')
      } finally {
        if (active && artifactPreviewRequestRef.current === requestId) setArtifactLoading(false)
      }
    }

    void loadArtifactPreview()
    return () => {
      active = false
    }
  }, [artifactPage, artifactPageRunOverrides, artifactViewer, jobId])

  useEffect(() => {
    setArtifactImageAgentLoading(false)
    setArtifactImageAgentError(null)
    setArtifactImageAgentPanelOpen(false)
  }, [artifactPage])

  useEffect(() => {
    if (artifactPage === null) return
    const strip = artifactThumbStripRef.current
    const target = strip?.querySelector<HTMLElement>(`[data-artifact-page="${artifactPage}"]`)
    target?.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' })
  }, [artifactPage, artifactViewer])

  const selectedPageSet = useMemo(() => new Set(selectedPages), [selectedPages])
  const selectedRange = useMemo(() => compressPages(selectedPages), [selectedPages])
  const usableOutline = useMemo(() => session?.outline ?? [], [session?.outline])
  const hasUsableOutline = usableOutline.length > 0
  const artifactPreviewJsonText = useMemo(
    () => (artifactPreviewData?.page_ir ? JSON.stringify(artifactPreviewData.page_ir, null, 2) : ''),
    [artifactPreviewData?.page_ir],
  )
  const imageAgentEnabled = Boolean(job?.image_agent?.enabled)
  const artifactImageAgent = useMemo(() => readArtifactImageAgentMeta(artifactPreviewData), [artifactPreviewData])
  const artifactCanGenerateImageAgent = Boolean(imageAgentEnabled && artifactPreviewData?.image_content_detected)
  const artifactHasImageAgentAction = Boolean(
    artifactCanGenerateImageAgent ||
      artifactImageAgent ||
      artifactImageAgentError ||
      artifactImageAgentLoading ||
      artifactPreviewData?.image_agent_empty,
  )
  const artifactCopyText = useMemo(
    () =>
      artifactImageAgentPanelOpen
        ? artifactImageAgent?.interpretationMarkdown || artifactImageAgent?.altText || ''
        : artifactViewMode === 'json'
        ? artifactPreviewJsonText
        : artifactPreviewData?.page_markdown?.trim() || '',
    [artifactPreviewData?.page_markdown, artifactPreviewJsonText, artifactViewMode, artifactImageAgent, artifactImageAgentPanelOpen],
  )
  const artifactHasMarkdown = Boolean(artifactViewer)
  const artifactHasJson = Boolean(artifactViewer)
  const artifactDownloadHref = artifactViewer
    ? apiUrl(`/api/jobs/${artifactViewer.sourceJobId ?? jobId}/download-output.zip`)
    : null
  const artifactSourcePages = artifactViewer?.pageNumbers ?? []
  const artifactNavigationPages = artifactViewer?.navigationPageNumbers ?? artifactSourcePages
  const artifactPageCursor = artifactPage !== null ? artifactNavigationPages.indexOf(artifactPage) : -1
  const artifactHasPrevPage = artifactPageCursor > 0
  const artifactHasNextPage = artifactPageCursor >= 0 && artifactPageCursor < artifactNavigationPages.length - 1
  const artifactImageDisplayBox = useMemo(() => {
    if (!artifactImageNaturalSize || !artifactImageViewportSize) return null
    const padding = 32
    const availableWidth = Math.max(0, artifactImageViewportSize.width - padding)
    const availableHeight = Math.max(0, artifactImageViewportSize.height - padding)
    if (!availableWidth || !availableHeight) return null

    const quarterTurn = artifactImageRotation % 180 !== 0
    const rotatedWidth = quarterTurn ? artifactImageNaturalSize.height : artifactImageNaturalSize.width
    const rotatedHeight = quarterTurn ? artifactImageNaturalSize.width : artifactImageNaturalSize.height
    const scale = Math.min(1, availableWidth / rotatedWidth, availableHeight / rotatedHeight)

    return {
      frameWidth: Math.max(1, Math.round(rotatedWidth * scale)),
      frameHeight: Math.max(1, Math.round(rotatedHeight * scale)),
      imageWidth: Math.max(1, Math.round(artifactImageNaturalSize.width * scale)),
      imageHeight: Math.max(1, Math.round(artifactImageNaturalSize.height * scale)),
    }
  }, [artifactImageNaturalSize, artifactImageRotation, artifactImageViewportSize])
  useEffect(() => {
    if (!artifactViewer) return

    function handleKeydown(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null
      const tagName = target?.tagName
      if (tagName === 'INPUT' || tagName === 'TEXTAREA' || tagName === 'SELECT' || target?.isContentEditable) return

      if (event.key === 'ArrowLeft' && artifactHasPrevPage) {
        event.preventDefault()
        setArtifactPage(artifactNavigationPages[artifactPageCursor - 1])
      }
      if (event.key === 'ArrowRight' && artifactHasNextPage) {
        event.preventDefault()
        setArtifactPage(artifactNavigationPages[artifactPageCursor + 1])
      }
    }

    window.addEventListener('keydown', handleKeydown)
    return () => {
      window.removeEventListener('keydown', handleKeydown)
    }
  }, [artifactHasNextPage, artifactHasPrevPage, artifactNavigationPages, artifactPageCursor, artifactViewer])
  const status = job?.status ?? 'idle'
  const runLocked = job?.status === 'running' || isSubmitting
  const elapsedLabel = formatElapsed(job?.started_at)
  const progressDisplayValue = Math.round(displayedRunProgress)
  const effectiveRunMode = job?.run_mode ?? 'fast'
  const effectiveRunLabel = getRunModeLabel(effectiveRunMode)
  const latestRunSelectionSummary = useMemo(
    () => describeRunSelection(job?.selection_mode, job?.selection, session?.page_count),
    [job?.selection, job?.selection_mode, session?.page_count],
  )
  const completedHistoryEntries = useMemo(
    () => runHistory.filter((entry) => entry.status === 'completed' && runHistoryHasArtifacts(entry)),
    [runHistory],
  )
  const fastExtractedPageSet = useMemo(() => {
    const set = new Set<number>()
    for (const entry of completedHistoryEntries) {
      if (entry.run_mode !== 'fast') continue
      for (const pageNumber of getHistoryEntryPages(entry, session?.page_count ?? 0, usableOutline)) {
        set.add(pageNumber)
      }
    }
    return set
  }, [completedHistoryEntries, session?.page_count, usableOutline])
  const repairedPageSet = useMemo(() => {
    const set = new Set<number>()
    for (const entry of completedHistoryEntries) {
      if (entry.run_mode !== 'reliable') continue
      if (entry.repair_engine_version !== currentRepairEngineVersion) continue
      for (const pageNumber of getHistoryEntryPages(entry, session?.page_count ?? 0, usableOutline)) {
        set.add(pageNumber)
      }
    }
    return set
  }, [completedHistoryEntries, session?.page_count, usableOutline])
  const fastSelectablePages = useMemo(() => {
    if (!session) return []
    return Array.from({ length: session.page_count }, (_, index) => index + 1).filter((pageNumber) => !fastExtractedPageSet.has(pageNumber))
  }, [fastExtractedPageSet, session])
  const fastSelectablePageSet = useMemo(() => new Set(fastSelectablePages), [fastSelectablePages])
  const currentSelectionPages = useMemo(() => {
    if (!session) return []
    if (selectionMode === 'all') {
      return Array.from({ length: session.page_count }, (_, index) => index + 1)
    }
    return selectedPages
  }, [selectedPages, selectionMode, session])
  const runnableFastSelectionPages = useMemo(
    () => currentSelectionPages.filter((pageNumber) => !fastExtractedPageSet.has(pageNumber)),
    [currentSelectionPages, fastExtractedPageSet],
  )
  const selectionSummary = useMemo(() => {
    if (!session) return 'No document loaded'
    if (selectionMode === 'all') {
      if (!runnableFastSelectionPages.length) return 'All extracted'
      if (runnableFastSelectionPages.length !== session.page_count) return `All remaining ${formatPageCountLabel(runnableFastSelectionPages.length)}`
      return `All ${formatPageCountLabel(session.page_count)}`
    }
    if (selectionMode === 'outline') {
      if (!selectedOutlineIds.length) return 'No sections selected'
      return `${selectedOutlineIds.length} section${selectedOutlineIds.length > 1 ? 's' : ''} · ${formatPageCountLabel(selectedPages.length)}`
    }
    if (!selectedPages.length) return 'No pages selected'
    if (selectedPages.length === 1) return `Page ${selectedPages[0]}`
    return `${formatPageCountLabel(selectedPages.length)} · ${selectedRange}`
  }, [runnableFastSelectionPages.length, selectedOutlineIds.length, selectedPages, selectedRange, selectionMode, session])
  const latestCompletedEntry = useMemo(() => completedHistoryEntries[0] ?? null, [completedHistoryEntries])
  const latestWholeDocumentRun = useMemo(
    () =>
      completedHistoryEntries.find((entry) => {
        const pages = getHistoryEntryPages(entry, session?.page_count ?? 0, usableOutline)
        return (session?.page_count ?? 0) > 0 && pages.length === (session?.page_count ?? 0)
      }) ?? null,
    [completedHistoryEntries, session?.page_count, usableOutline],
  )
  const pageEffectiveRunMap = useMemo(() => {
    const map = new Map<number, RunHistoryEntry>()
    for (const entry of completedHistoryEntries) {
      const pages = getHistoryEntryPages(entry, session?.page_count ?? 0, usableOutline)
      for (const page of pages) {
        if (!map.has(page)) map.set(page, entry)
      }
    }
    return map
  }, [completedHistoryEntries, session?.page_count, usableOutline])
  const latestOutputPages = useMemo(() => {
    if (latestWholeDocumentRun) {
      return getHistoryEntryPages(latestWholeDocumentRun, session?.page_count ?? 0, usableOutline)
    }
    return Array.from(pageEffectiveRunMap.keys()).sort((left, right) => left - right)
  }, [latestWholeDocumentRun, pageEffectiveRunMap, session?.page_count, usableOutline])
  const latestOutputSubtitle = useMemo(() => {
    if (latestWholeDocumentRun && session) return compactViewerSubtitle(`All ${session.page_count} pages`)
    if (latestOutputPages.length > 0) {
      return compactViewerSubtitle(
        latestOutputPages.length <= 12 ? `Pages ${compressPages(latestOutputPages)}` : formatPageCountLabel(latestOutputPages.length),
      )
    }
    return compactViewerSubtitle('Latest output')
  }, [latestOutputPages, latestWholeDocumentRun, session])
  const latestOutputDurationLabel = useMemo(() => {
    const entry = latestWholeDocumentRun ?? latestCompletedEntry
    return formatDurationLabel(
      entry?.duration_sec ?? job?.duration_sec ?? durationBetweenSeconds(job?.started_at, job?.finished_at),
    )
  }, [job?.duration_sec, job?.finished_at, job?.started_at, latestCompletedEntry, latestWholeDocumentRun])
  const latestOutputMetaItems = useMemo(() => {
    if (!latestOutputPages.length) return []
    const items = [
      latestWholeDocumentRun && session ? `All ${session.page_count} pages` : formatPageCountLabel(latestOutputPages.length),
    ]
    if (latestOutputDurationLabel) items.push(`Run ${latestOutputDurationLabel}`)
    const repairedCount = latestOutputPages.filter((pageNumber) => repairedPageSet.has(pageNumber)).length
    if (repairedCount > 0) items.push(`${repairedCount} repaired`)
    if (imageAgentEnabled) items.push('Image Agent')
    return items
  }, [latestOutputDurationLabel, latestOutputPages, latestWholeDocumentRun, repairedPageSet, session, imageAgentEnabled])
  const currentFileVersion = useMemo(
    () => fileHistory?.versions.find((version) => version.is_current) ?? null,
    [fileHistory],
  )
  const currentFileExtractionHistory = useMemo(
    () =>
      (currentFileVersion?.runs ?? runHistory).filter(
        (entry) => entry.status === 'completed' && runHistoryHasArtifacts(entry),
      ),
    [currentFileVersion?.runs, runHistory],
  )
  const previousFileVersions = useMemo(
    () => (fileHistory?.versions ?? []).filter((version) => !version.is_current),
    [fileHistory],
  )
  const hasCurrentOutputs = latestOutputPages.length > 0
  const outputUpToDate = hasCurrentOutputs && arePageListsEqual(currentSelectionPages, latestOutputPages)
  const openOutputHint = useMemo(() => {
    if (!hasCurrentOutputs) return null
    if (!outputUpToDate) return 'Merged from previous selection'
    if (latestWholeDocumentRun && session) return 'Merged final output'
    return 'Merged from extracted pages'
  }, [hasCurrentOutputs, latestWholeDocumentRun, outputUpToDate, session])
  const pageCount = session?.page_count ?? 0
  const selectionBadgeCount =
    selectionMode === 'all' && fastExtractedPageSet.size > 0 ? runnableFastSelectionPages.length : currentSelectionPages.length
  const showSelectedCount = runnableFastSelectionPages.length > 0 && runnableFastSelectionPages.length < pageCount
  const currentRunsExpanded = expandedHistoryVersions.includes(CURRENT_HISTORY_KEY)
  const hasSelection = currentSelectionPages.length > 0
  const nothingNewToRun = !runLocked && hasSelection && !runnableFastSelectionPages.length
  const currentWorkflowStep = useMemo(() => {
    if (!session) return 1
    if (status === 'running') return 3
    if (hasSelection && !outputUpToDate) return 3
    if (outputUpToDate) return 4
    if (hasSelection) return 3
    return 2
  }, [hasSelection, outputUpToDate, session, status])

  const sectionModeHint = useMemo(() => (hasUsableOutline ? 'Outline' : 'No outline'), [hasUsableOutline])

  const mergedDocumentPageRunIds = useMemo(() => {
    const result: Record<number, string | null | undefined> = {}
    for (const page of latestOutputPages) {
      result[page] = pageEffectiveRunMap.get(page)?.run_id ?? latestWholeDocumentRun?.run_id ?? latestCompletedEntry?.run_id ?? job?.run_id ?? null
    }
    return result
  }, [job?.run_id, latestCompletedEntry?.run_id, latestOutputPages, latestWholeDocumentRun, pageEffectiveRunMap])
  const artifactRepairAllowed = artifactViewer?.allowRepair === true
  const artifactPageHasOutput =
    artifactPage !== null &&
    Boolean(
      artifactViewer?.pageRunIds?.[artifactPage] ??
        (artifactRepairAllowed ? pageEffectiveRunMap.get(artifactPage)?.run_id : null),
    )
  const artifactPageAlreadyRepaired =
    artifactRepairAllowed &&
    artifactPage !== null &&
    (repairedPageSet.has(artifactPage) || Boolean(artifactPageRunOverrides[artifactPage]))
  const currentViewerRepairTask =
    artifactPage !== null && viewerRepairTask?.pageNumber === artifactPage ? viewerRepairTask : null
  const artifactRepairInProgress =
    Boolean(viewerRepairTask && viewerRepairTask.phase !== 'done' && viewerRepairTask.phase !== 'error') ||
    artifactRepairingPage !== null
  const artifactCurrentPageRepairing = Boolean(
    currentViewerRepairTask &&
      currentViewerRepairTask.phase !== 'done' &&
      currentViewerRepairTask.phase !== 'error',
  )
  const artifactCurrentPageRepairDone = currentViewerRepairTask?.phase === 'done'
  const artifactRepairButtonLabel = artifactCurrentPageRepairing
    ? currentViewerRepairTask?.phase === 'refreshing'
      ? 'Refreshing'
      : 'Repairing'
    : artifactCurrentPageRepairDone || artifactPageAlreadyRepaired
      ? 'Repaired'
      : 'Repair page'
  const artifactRepairStatusLabel =
    currentViewerRepairTask?.phase === 'starting'
      ? 'Starting'
      : currentViewerRepairTask?.phase === 'repairing'
        ? 'Repairing'
        : currentViewerRepairTask?.phase === 'refreshing'
          ? 'Updating output'
          : currentViewerRepairTask?.phase === 'done'
            ? 'Updated'
            : currentViewerRepairTask?.phase === 'error'
              ? 'Repair failed'
              : null
  const artifactRepairStatusBody =
    currentViewerRepairTask?.phase === 'error' ? currentViewerRepairTask.message ?? 'Repair failed. Please try again.' : null

  useEffect(() => {
    const previousStatus = previousStatusRef.current
    const currentStatus = job?.status ?? 'idle'
    const completionSignature =
      job?.finished_at && job?.output_dir ? `${job.finished_at}:${job.output_dir}` : null

    if (currentStatus === 'running' && previousStatus !== 'running') {
      setExpandedWorkflowStep(3)
    }

    if (
      (currentStatus === 'completed' || currentStatus === 'failed' || currentStatus === 'canceled') &&
      previousStatus === 'running'
    ) {
      setExpandedWorkflowStep(4)
    }

    if (currentStatus === 'completed' && completionSignature && lastCelebratedSignatureRef.current !== completionSignature) {
      lastCelebratedSignatureRef.current = completionSignature
      if (job?.run_mode === 'reliable') {
        previousStatusRef.current = currentStatus
        return
      }
      const cleanup = launchCompletionCelebration()

      previousStatusRef.current = currentStatus
      return cleanup
    }

    previousStatusRef.current = currentStatus
  }, [
    hasCurrentOutputs,
    job?.artifacts,
    job?.finished_at,
    job?.output_dir,
    job?.run_mode,
    job?.status,
  ])

  async function copyToClipboard(value: string, key: string) {
    if (!value.trim()) return
    try {
      await navigator.clipboard.writeText(value)
      setCopiedSurface(key)
      window.setTimeout(() => {
        setCopiedSurface((current) => (current === key ? null : current))
      }, 1400)
    } catch {
      // Ignore clipboard failures on unsupported environments.
    }
  }

  function openArtifactViewer(options: ArtifactViewerState) {
    setArtifactViewer(options)
  }

  function openPageCompare(pageNumber: number) {
    const effectiveRun = pageEffectiveRunMap.get(pageNumber)
    const allSourcePages = session?.pages.map((page) => page.page_index + 1) ?? [pageNumber]
    openArtifactViewer({
      title: 'Output',
      sourceJobId: jobId,
      pageNumbers: [pageNumber],
      navigationPageNumbers: allSourcePages,
      pageRunIds: {
        ...mergedDocumentPageRunIds,
        [pageNumber]: effectiveRun?.run_id ?? mergedDocumentPageRunIds[pageNumber] ?? null,
      },
      initialPage: pageNumber,
      initialTab: 'markdown',
      allowRepair: true,
    })
  }

  async function handleCancelRun() {
    if (!jobId || job?.status !== 'running' || cancelingRun) return
    try {
      setCancelingRun(true)
      const response = await fetch(apiUrl(`/api/jobs/${jobId}/cancel`), {
        method: 'POST',
      })
      if (!response.ok) {
        throw new Error(await readResponseErrorMessage(response, 'Failed to cancel extraction.'))
      }
      const payload = (await response.json()) as JobSnapshot
      setJob(payload)
    } catch (caught) {
      setLoadError(caught instanceof Error ? caught.message : 'Failed to cancel extraction.')
    } finally {
      setCancelingRun(false)
    }
  }

  async function handleUpload(file: File) {
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      setUploadError('Upload a PDF file.')
      return
    }

    try {
      setUploading(true)
      setUploadError(null)
      setLoadError(null)
      setArtifactViewer(null)
      const formData = new FormData()
      formData.append('file', file, file.name)
      if (uploadIntent === 'replace' && jobId) formData.append('replaces_job_id', jobId)
      const response = await fetch(apiUrl('/api/upload'), {
        method: 'POST',
        body: formData,
      })
      if (!response.ok) {
        throw new Error(await readResponseErrorMessage(response, 'Upload failed.'))
      }
      const payload = (await response.json()) as UploadResponse
      setSession(payload.session)
      setJob(payload.session.job)
      const bootstrap = selectionBootstrap(payload.session)
      setSelectionMode(bootstrap.mode)
      setSelectedPages(bootstrap.selectedPages)
      setSelectedOutlineIds(bootstrap.selectedOutlineIds)
      setPageInput(bootstrap.pageInput)
      setOutputDir(payload.session.default_output_dir)
      setJobId(payload.job_id)
      setExpandedWorkflowStep(2)
    } catch (caught) {
      setUploadError(caught instanceof Error ? caught.message : 'Upload failed.')
    } finally {
      setUploadIntent('new')
      setUploading(false)
    }
  }

  function openFilePicker(intent: 'new' | 'replace' = 'new') {
    if (uploading || runLocked) return
    setUploadIntent(intent)
    fileInputRef.current?.click()
  }

  function switchMode(mode: SelectionMode) {
    if (!session) return
    setSelectionMode(mode)
    setPageInputError(null)
    if (mode === 'all') {
      const pages = session.pages.map((page) => page.page_index + 1)
      setSelectedPages(pages)
      setSelectedOutlineIds([])
      setPageInput(compressPages(pages))
      return
    }
    if (mode === 'outline') {
      if (!usableOutline.length) return
      const chosenIds = selectedOutlineIds.length ? selectedOutlineIds : [usableOutline[0].id]
      const chosen = usableOutline.filter((item) => chosenIds.includes(item.id))
      const pages = [...new Set(chosen.flatMap((item) => getOutlineRange(item, usableOutline, session.page_count)))]
        .filter((pageNumber) => !fastExtractedPageSet.has(pageNumber))
        .sort((a, b) => a - b)
      setSelectedOutlineIds(chosenIds)
      setSelectedPages(pages)
      setPageInput(compressPages(pages))
      return
    }
    setSelectedOutlineIds([])
    setPageInput(selectedPages.length ? compressPages(selectedPages) : '')
  }

  function toggleOutline(id: number) {
    if (!session) return
    setSelectionMode('outline')
    setSelectedOutlineIds((current) => {
      const next = current.includes(id) ? current.filter((value) => value !== id) : [...current, id].sort((a, b) => a - b)
      const chosen = usableOutline.filter((item) => next.includes(item.id))
      const pages = [...new Set(chosen.flatMap((item) => getOutlineRange(item, usableOutline, session.page_count)))]
        .filter((pageNumber) => !fastExtractedPageSet.has(pageNumber))
        .sort((a, b) => a - b)
      setSelectedPages(pages)
      setPageInput(compressPages(pages))
      return next
    })
  }

  function togglePage(pageNumber: number) {
    if (!session) return
    setSelectionMode('pagerange')
    setSelectedOutlineIds([])
    setSelectedPages((current) => {
      const next =
        selectionMode === 'all'
          ? new Set(runnableFastSelectionPages)
          : new Set(current)
      if (next.has(pageNumber)) next.delete(pageNumber)
      else next.add(pageNumber)
      const pages = [...next].sort((a, b) => a - b)
      setPageInput(compressPages(pages))
      setPageInputError(null)
      return pages
    })
  }

  function applyPageInput(raw: string) {
    if (!session) return
    setPageInput(raw)
    if (!raw.trim()) {
      setSelectedPages([])
      setPageInputError(null)
      return
    }
    try {
      const pages = parsePageRange(raw, session.page_count)
      const blockedPages = pages.filter((pageNumber) => fastExtractedPageSet.has(pageNumber))
      const allowedPages = pages.filter((pageNumber) => !fastExtractedPageSet.has(pageNumber))
      setSelectedPages(allowedPages)
      setPageInputError(
        blockedPages.length ? `Fast extraction already exists for pages ${compressPages(blockedPages)}.` : null,
      )
    } catch (caught) {
      setPageInputError(caught instanceof Error ? caught.message : 'Invalid page range.')
    }
  }

  function buildCurrentSelectionRequest() {
    if (!session) return null

    if (selectionMode === 'pagerange') {
      if (!selectedRange) {
        setPageInputError('Pick at least one page before running.')
        return null
      }
      return {
        selection_mode: 'pagerange' as SelectionMode,
        selection: selectedRange,
      }
    }

    if (selectionMode === 'outline') {
      if (!selectedOutlineIds.length) {
        setLoadError('Choose at least one outline section before running.')
        return null
      }
      return {
        selection_mode: 'outline' as SelectionMode,
        selection: selectedOutlineIds.join(','),
      }
    }

    if (!runnableFastSelectionPages.length) {
      setLoadError('All pages already have fast output.')
      return null
    }

    return {
      selection_mode:
        currentSelectionPages.length === session.page_count && fastExtractedPageSet.size === 0
          ? ('all' as SelectionMode)
          : ('pagerange' as SelectionMode),
      selection:
        currentSelectionPages.length === session.page_count && fastExtractedPageSet.size === 0
          ? 'all'
          : compressPages(runnableFastSelectionPages),
    }
  }

  async function startRun(options: {
    runMode: RunMode
    selectionMode: SelectionMode
    selection: string
    onBeforeRun?: () => void
  }) {
    if (!session || !jobId) return

    try {
      setIsSubmitting(true)
      setLoadError(null)
      options.onBeforeRun?.()
      const response = await fetch(apiUrl(`/api/jobs/${jobId}/run`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          selection_mode: options.selectionMode,
          selection: options.selection,
          output_dir: outputDir,
          run_mode: options.runMode,
        }),
      })
      if (!response.ok) {
        throw new Error(await response.text())
      }
      const payload = (await response.json()) as JobSnapshot
      setJob(payload)
    } catch (caught) {
      setLoadError(caught instanceof Error ? caught.message : 'Failed to start pipeline.')
    } finally {
      setIsSubmitting(false)
    }
  }

  async function handleRun(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const request = buildCurrentSelectionRequest()
    if (!request) return
    await startRun({
      runMode: 'fast',
      selectionMode: request.selection_mode,
      selection: request.selection,
    })
  }

  async function handleRepairPage(pageNumber: number) {
    if (!jobId || !session || !pageNumber) return
    if (repairedPageSet.has(pageNumber)) {
      setRepairActionError('This page already uses the repaired output.')
      return
    }
    if (viewerRepairTask && viewerRepairTask.phase !== 'done' && viewerRepairTask.phase !== 'error') {
      return
    }

    const startedAt = Date.now()
    try {
      setIsSubmitting(true)
      setLoadError(null)
      setRepairActionError(null)
      setArtifactPreviewError(null)
      setViewerRepairTask({
        pageNumber,
        phase: 'starting',
        startedAt,
        message: 'Starting repair',
      })
      setSelectionMode('pagerange')
      setSelectedOutlineIds([])
      setSelectedPages([pageNumber])
      setPageInput(String(pageNumber))
      setPageInputError(null)
      setArtifactRepairingPage(pageNumber)
      lastRepairRefreshRunIdRef.current = null

      const response = await fetch(apiUrl(`/api/jobs/${jobId}/run`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          selection_mode: 'pagerange',
          selection: String(pageNumber),
          output_dir: outputDir,
          run_mode: 'reliable',
        }),
      })
      if (!response.ok) {
        throw new Error(await readResponseErrorMessage(response, 'Failed to start page repair.'))
      }
      const payload = (await response.json()) as JobSnapshot
      setJob(payload)
      const repairRunId = payload.run_id ?? null
      setViewerRepairTask((current) =>
        current?.startedAt === startedAt
          ? {
              ...current,
              phase: 'repairing',
              runId: repairRunId,
              message: 'Repair is running',
            }
          : current,
      )
    } catch (caught) {
      setArtifactRepairingPage(null)
      const message = caught instanceof Error ? caught.message : 'Failed to start page repair.'
      setViewerRepairTask((current) =>
        current?.startedAt === startedAt
          ? {
              ...current,
              phase: 'error',
              message,
            }
          : current,
      )
    } finally {
      setIsSubmitting(false)
    }
  }

  async function handleGenerateImageAgent(pageNumber: number) {
    const previewJobId = artifactViewer?.sourceJobId ?? jobId
    if (!previewJobId || !artifactViewer || !pageNumber) return

    const pageRunId = artifactViewer.pageRunIds?.[pageNumber] ?? artifactViewer.runId ?? null

    try {
      setArtifactImageAgentLoading(true)
      setArtifactImageAgentError(null)
      const response = await fetch(apiUrl(`/api/jobs/${previewJobId}/image-agent`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          page: pageNumber,
          run_id: pageRunId,
        }),
      })
      if (!response.ok) {
        throw new Error(await readResponseErrorMessage(response, 'Image Agent could not interpret this page.'))
      }
      const payload = (await response.json()) as Partial<PagePreview>
      setArtifactPreviewData((current) => (current ? { ...current, ...payload } : (payload as PagePreview)))
      launchCompletionCelebration()
    } catch (caught) {
      setArtifactImageAgentError(
        caught instanceof Error ? caught.message : 'Image Agent could not interpret this page.',
      )
    } finally {
      setArtifactImageAgentLoading(false)
    }
  }

  function openArtifactImageAgentPanel() {
    setArtifactImageAgentPanelOpen(true)
    if (
      !artifactImageAgent &&
      !artifactImageAgentLoading &&
      !artifactPreviewData?.image_agent_empty &&
      imageAgentEnabled &&
      artifactPage !== null
    ) {
      void handleGenerateImageAgent(artifactPage)
    }
  }

  function openRunHistoryViewer(versionJobId: string, entry: RunHistoryEntry, pageCount: number) {
    const markdownHref = resolveBackendHref(entry.artifact_urls.document_md ?? entry.artifact_urls['document.md'])
    const jsonHref = resolveBackendHref(entry.artifact_urls.document_ir_json ?? entry.artifact_urls['document_ir.json'])
    const pageNumbers = getHistoryEntryPages(entry, pageCount, [])
    if (!markdownHref && !jsonHref) return

    openArtifactViewer({
      title: 'Output',
      subtitle: compactViewerSubtitle(describeHistorySelection(entry)),
      sourceJobId: versionJobId,
      runId: entry.run_id ?? null,
      pageNumbers,
      pageRunIds: Object.fromEntries(pageNumbers.map((pageNumber) => [pageNumber, entry.run_id ?? null])),
      initialPage: pageNumbers[0] ?? 1,
      markdownHref: markdownHref || undefined,
      jsonHref: jsonHref || undefined,
      initialTab: markdownHref ? 'markdown' : 'json',
      allowRepair: false,
    })
  }

  function openFileVersionLatestViewer(version: FileVersionHistoryEntry) {
    const markdownHref = resolveBackendHref(version.merged_artifact_urls.document_md ?? version.merged_artifact_urls['document.md'])
    const jsonHref = resolveBackendHref(version.merged_artifact_urls.document_ir_json ?? version.merged_artifact_urls['document_ir.json'])
    if (!markdownHref && !jsonHref) return

    openArtifactViewer({
      title: 'Output',
      subtitle: compactViewerSubtitle(version.filename),
      sourceJobId: version.job_id,
      runId: null,
      pageNumbers: version.latest_output_pages,
      pageRunIds: version.effective_page_run_ids,
      initialPage: version.latest_output_pages[0] ?? 1,
      markdownHref: markdownHref || undefined,
      jsonHref: jsonHref || undefined,
      initialTab: markdownHref ? 'markdown' : 'json',
      allowRepair: false,
    })
  }

  function toggleHistoryVersion(jobIdToToggle: string) {
    setExpandedHistoryVersions((current) =>
      current.includes(jobIdToToggle)
        ? current.filter((value) => value !== jobIdToToggle)
        : [...current, jobIdToToggle],
    )
  }

  const showEmptyWorkspace = !jobId
  const showLoadingWorkspace = Boolean(jobId && (loadingSession || !session))
  const showCurrentRuns =
    currentFileExtractionHistory.length > 1 ||
    currentFileExtractionHistory.some((entry) => entry.run_mode === 'reliable')
  const hasPreviousFiles = previousFileVersions.length > 0

  function handleGoHome() {
    setArtifactViewer(null)
    setArtifactPage(null)
    setArtifactPreviewData(null)
    setArtifactPreviewError(null)
    setArtifactImageAgentError(null)
    setArtifactImageAgentPanelOpen(false)
    setUploadError(null)
    setLoadError(null)
    setExpandedWorkflowStep(1)
    setJobId(null)
  }

  return (
    <div className="app-shell min-h-screen text-slate-900">
      <div aria-hidden="true" className="app-atmosphere">
        <div className="app-atmosphere-sweep app-atmosphere-sweep--one" />
        <div className="app-atmosphere-sweep app-atmosphere-sweep--two" />
        <div className="app-atmosphere-ribbon app-atmosphere-ribbon--one" />
        <div className="app-atmosphere-ribbon app-atmosphere-ribbon--two" />
        <div className="app-atmosphere-orb app-atmosphere-orb--one" />
        <div className="app-atmosphere-orb app-atmosphere-orb--two" />
        <div className="app-atmosphere-orb app-atmosphere-orb--three" />
        <div className="app-atmosphere-orb app-atmosphere-orb--four" />
        <div className="app-atmosphere-grid" />
      </div>
      <div className="relative z-[1] mx-auto max-w-[1800px] px-5 py-5">
        <TopBar
          onHome={handleGoHome}
          rightSlot={showEmptyWorkspace ? undefined : (
            <button
              type="button"
              onClick={() => openFilePicker('new')}
              disabled={uploading || runLocked}
              className="inline-flex min-w-[126px] shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-full border border-slate-300/90 bg-white px-4.5 py-2.5 text-sm font-semibold tracking-[-0.01em] text-slate-800 shadow-[0_12px_28px_rgba(15,23,42,0.06)] backdrop-blur transition hover:border-[color:var(--theme-primary)]/28 hover:bg-white disabled:cursor-not-allowed disabled:opacity-55"
            >
              {uploading ? <LoaderCircle className="h-4 w-4 animate-spin text-[color:var(--theme-primary)]" /> : <Upload className="h-4 w-4 text-[color:var(--theme-primary)]" />}
              {uploading ? 'Preparing…' : 'New PDF'}
            </button>
          )}
        />

        <input
          ref={fileInputRef}
          type="file"
          accept="application/pdf,.pdf"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0]
            if (file) void handleUpload(file)
            event.currentTarget.value = ''
          }}
        />

        {uploadError && !showEmptyWorkspace && (
          <div className="mt-4 rounded-[22px] border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700 shadow-[0_10px_24px_rgba(15,23,42,0.04)]">
            {uploadError}
          </div>
        )}

        {showEmptyWorkspace ? (
          <div className="mt-4 grid min-h-[calc(100vh-7.5rem)] gap-5 items-stretch xl:[grid-auto-rows:1fr] xl:grid-cols-[300px_minmax(0,1fr)]">
            <section className="flex h-full min-h-0 flex-col rounded-[30px] border border-slate-300/80 bg-white/88 p-5 shadow-[0_18px_44px_rgba(15,23,42,0.06)] ring-1 ring-white/60 backdrop-blur">
              <div className="relative space-y-3 before:absolute before:bottom-8 before:left-5 before:top-8 before:w-px before:bg-[linear-gradient(180deg,rgba(0,77,64,0.2),rgba(0,77,64,0.02))]">
                <WorkflowSection
                  step="01"
                  title="Upload"
                  detail={uploading ? 'Preparing document' : 'Choose one PDF'}
                  state="active"
                  open={false}
                />
                <WorkflowSection step="02" title="Choose pages" detail="After upload" state="pending" open={false} />
                <WorkflowSection step="03" title="Run" detail="After selection" state="pending" open={false} />
                <WorkflowSection step="04" title="Inspect" detail="After a run" state="pending" open={false} />
              </div>
              {uploadError && <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{uploadError}</div>}
            </section>

            <main className="min-w-0 h-full min-h-0">
              <section className="flex h-full min-h-0 flex-col rounded-[30px] border border-slate-300/80 bg-white/92 px-6 py-6 text-slate-900 shadow-[0_18px_44px_rgba(15,23,42,0.08)] ring-1 ring-white/60 backdrop-blur">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <div className="text-[11px] font-bold uppercase tracking-[0.22em] text-[color:var(--theme-primary)]">Upload</div>
                    <h1 className="mt-3 max-w-3xl font-['Iowan_Old_Style','Palatino_Linotype','Book_Antiqua',serif] text-[1.7rem] font-semibold leading-[1.02] tracking-[-0.04em] text-slate-950 md:text-[2rem]">
                      Upload PDF
                    </h1>
                    <p className="mt-3 max-w-2xl text-[15px] leading-7 text-slate-600">One file to start</p>
                  </div>
                </div>

                <div className="mt-6 flex min-h-0 flex-1">
                  <button
                    type="button"
                    onClick={() => openFilePicker('new')}
                    disabled={uploading}
                    className="group flex min-h-full w-full flex-1 items-center justify-center rounded-[28px] border border-dashed border-slate-300 bg-[linear-gradient(180deg,rgba(255,255,255,0.92),rgba(240,245,239,0.84))] px-6 py-12 text-center shadow-[inset_0_1px_0_rgba(255,255,255,0.6)] transition hover:border-[color:var(--theme-primary)]/24 hover:bg-[linear-gradient(180deg,rgba(255,255,255,0.96),rgba(236,244,239,0.9))] disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    <div className="max-w-xl">
                      <div className="mx-auto inline-flex h-16 w-16 items-center justify-center rounded-full border border-slate-200 bg-white shadow-[0_14px_28px_rgba(15,23,42,0.06)] transition group-hover:scale-[1.02]">
                        {uploading ? <LoaderCircle className="h-7 w-7 animate-spin text-[color:var(--theme-primary)]" /> : <Upload className="h-7 w-7 text-[color:var(--theme-primary)]" />}
                      </div>
                      <div className="mt-6 text-xl font-semibold text-slate-900">{uploading ? 'Preparing file' : 'Choose file'}</div>
                      <div className="mt-6 inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-4 py-2 text-sm font-semibold text-[color:var(--theme-primary)] shadow-[0_10px_24px_rgba(15,23,42,0.04)]">
                        <Upload className="h-4 w-4" />
                        {uploading ? 'Preparing…' : 'Upload PDF'}
                      </div>
                    </div>
                  </button>
                </div>
              </section>
            </main>
          </div>
        ) : showLoadingWorkspace ? (
          <div className="mx-auto flex min-h-[calc(100vh-7rem)] max-w-3xl items-center justify-center py-10">
            <div className="flex w-full items-center gap-3 rounded-[28px] border border-slate-300/80 bg-white/92 px-6 py-6 text-sm text-slate-600 shadow-[0_18px_44px_rgba(15,23,42,0.08)] ring-1 ring-white/60">
              <LoaderCircle className="h-5 w-5 animate-spin text-[color:var(--theme-primary)]" />
              {loadError ?? 'Loading document'}
            </div>
          </div>
        ) : session ? (

        <div className="mt-4 grid gap-4 xl:grid-cols-[318px_minmax(0,1fr)] xl:items-start">
          <aside className="flex flex-col gap-4 xl:sticky xl:top-4 xl:self-start">
          <form onSubmit={handleRun} className="overflow-x-hidden rounded-[30px] border border-slate-300/80 bg-white/88 p-4 shadow-[0_14px_32px_rgba(15,23,42,0.05)] ring-1 ring-white/60 backdrop-blur">
            <div className="relative min-w-0 space-y-3 overflow-x-hidden before:absolute before:bottom-8 before:left-5 before:top-8 before:w-px before:bg-[linear-gradient(180deg,rgba(0,77,64,0.22),rgba(0,77,64,0.02))]">
              <WorkflowSection
                step="01"
                title="Upload"
                detail="File loaded"
                state="done"
                open={expandedWorkflowStep === 1}
                badge={undefined}
                onToggle={() => setExpandedWorkflowStep((current) => (current === 1 ? 0 : 1))}
              >
                <div className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-4">
                  <div className="text-sm font-semibold text-slate-900 break-words">{session.input_pdf_name}</div>
                </div>
                <button
                  type="button"
                  onClick={() => openFilePicker('replace')}
                  disabled={uploading || runLocked}
                  className="mt-4 inline-flex w-full items-center justify-center gap-2 rounded-full border border-slate-200 bg-white px-5 py-3 text-sm font-semibold text-slate-800 transition hover:border-[color:var(--theme-primary)]/25 hover:text-[color:var(--theme-primary)] disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {uploading ? <LoaderCircle className="h-4 w-4 animate-spin text-[color:var(--theme-primary)]" /> : <Upload className="h-4 w-4 text-[color:var(--theme-primary)]" />}
                  {uploading ? 'Preparing…' : 'Replace file'}
                </button>
              </WorkflowSection>

              <WorkflowSection
                step="02"
                title="Choose pages"
                detail={hasSelection ? selectionSummary : 'Pick pages'}
                state={hasSelection ? 'done' : currentWorkflowStep === 2 ? 'active' : 'pending'}
                open={expandedWorkflowStep === 2}
                badge={
                  selectionBadgeCount > 0 ? (
                    <span className="rounded-full bg-[color:var(--theme-secondary)]/16 px-2.5 py-1 text-[11px] font-semibold text-[color:var(--theme-secondary-strong)]">
                      {formatPageCountLabel(selectionBadgeCount)}
                    </span>
                  ) : undefined
                }
                onToggle={() => setExpandedWorkflowStep((current) => (current === 2 ? 0 : 2))}
              >
                <div className="text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">Selection</div>
                <div className="mt-3 grid gap-2.5">
                  <ModeCard label="All pages" hint="Whole document" checked={selectionMode === 'all'} disabled={runLocked} onSelect={() => switchMode('all')} />
                  <ModeCard
                    label="Sections"
                    hint={hasUsableOutline ? 'PDF outline' : sectionModeHint}
                    checked={selectionMode === 'outline'}
                    disabled={!hasUsableOutline || runLocked}
                    onSelect={() => switchMode('outline')}
                  />
                  <ModeCard label="Page range" hint="Range or click" checked={selectionMode === 'pagerange'} disabled={runLocked} onSelect={() => switchMode('pagerange')} />
                </div>

                <div className="mt-4 rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
                  <div className="text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">Current selection</div>
                  <div className="mt-2 text-sm font-semibold text-slate-900">{selectionSummary}</div>
                </div>

                {selectionMode === 'outline' && hasUsableOutline && (
                  <div className="mt-4 rounded-[24px] border border-[color:var(--theme-primary)]/18 bg-[linear-gradient(180deg,rgba(245,249,246,0.96),rgba(239,245,241,0.94))] px-4 py-4">
                    <div className="inline-flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">
                      <Layers3 className="h-3.5 w-3.5" />
                      Sections
                    </div>
                    <div className="mt-3 max-h-72 space-y-2 overflow-auto pr-1">
                      {usableOutline.map((item) => {
                        const active = selectedOutlineIds.includes(item.id)
                        return (
                          <button
                            key={item.id}
                            type="button"
                            onClick={() => toggleOutline(item.id)}
                            className={cn(
                              'relative w-full overflow-hidden rounded-2xl border px-3 py-3 text-left transition',
                              active
                                ? 'border-[color:var(--theme-primary)]/34 bg-[linear-gradient(180deg,rgba(255,255,255,0.98),rgba(241,247,243,0.98))] shadow-[0_12px_24px_rgba(0,77,64,0.08)] ring-1 ring-[color:var(--theme-primary)]/10 before:absolute before:bottom-0 before:left-0 before:top-0 before:w-[3px] before:bg-[linear-gradient(180deg,#0b3b34,#b2cb35)]'
                                : 'border-slate-200 bg-white/70 hover:border-slate-300',
                            )}
                            style={{ paddingLeft: `${12 + item.level * 14}px` }}
                          >
                            <div className={cn('text-sm font-medium text-slate-900', active && 'text-[color:var(--theme-primary-strong)]')}>{item.title}</div>
                            <div className={cn('mt-1 text-[11px] text-slate-500', active && 'text-slate-700')}>p.{item.page_index + 1}</div>
                          </button>
                        )
                      })}
                    </div>
                  </div>
                )}

                <div className="mt-4 rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
                  <label className="text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">Page range</label>
                  <input
                    value={pageInput}
                    onChange={(event) => applyPageInput(event.target.value)}
                    onFocus={() => selectionMode !== 'pagerange' && switchMode('pagerange')}
                    placeholder="e.g. 1-20,45-60"
                    disabled={runLocked}
                    className="mt-3 w-full rounded-2xl border border-slate-200 bg-white px-3.5 py-3 text-sm text-slate-900 outline-none transition focus:border-[color:var(--theme-primary)]"
                  />
                  <div className="mt-2 text-xs text-slate-500">Syncs with page picks.</div>
                  {pageInputError && <div className="mt-2 text-xs text-rose-500">{pageInputError}</div>}
                </div>
              </WorkflowSection>

              <WorkflowSection
                step="03"
                title="Run"
                detail={
                  status === 'running'
                    ? job?.run_mode === 'reliable'
                      ? 'Repair running'
                      : 'Running'
                    : outputUpToDate
                      ? 'Done'
                      : !fastSelectablePages.length
                        ? 'Nothing new to run'
                      : hasSelection
                        ? 'Ready'
                        : 'Choose pages first'
                }
                state={status === 'running' || (hasSelection && !outputUpToDate) ? 'active' : outputUpToDate ? 'done' : 'pending'}
                open={expandedWorkflowStep === 3}
                onToggle={() => setExpandedWorkflowStep((current) => (current === 3 ? 0 : 3))}
              >
                <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
                  <div className="text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">
                    {nothingNewToRun ? 'Current selection' : 'Ready to run'}
                  </div>
                  <div className="mt-2 text-sm font-semibold text-slate-900">{selectionSummary}</div>
                </div>

                {job?.status === 'running' && (
                  <div className="mt-4 rounded-[24px] border border-[color:var(--theme-primary)]/18 bg-[linear-gradient(180deg,rgba(245,249,246,0.98),rgba(237,244,240,0.96))] px-4 py-4 shadow-[0_16px_36px_rgba(0,77,64,0.08)]">
                    <div className="flex items-center justify-between gap-3">
                      <div className="text-[10px] font-bold uppercase tracking-[0.18em] text-[color:var(--theme-primary)]">
                        {job.run_mode === 'reliable' ? 'Repair running' : 'Running'}
                      </div>
                      <div className="text-xs font-semibold text-slate-600">{progressDisplayValue}%</div>
                    </div>
                    <div className="mt-3 h-2 overflow-hidden rounded-full bg-white shadow-[inset_0_1px_2px_rgba(15,23,42,0.08)]">
                      <div
                        className="h-full rounded-full bg-gradient-to-r from-[color:var(--theme-primary)] via-[#0d6a5c] to-[color:var(--theme-secondary)] transition-all duration-500"
                        style={{ width: `${displayedRunProgress}%` } as CSSProperties}
                      />
                    </div>
                    <div className="mt-3 flex flex-wrap items-center gap-2 text-xs font-medium text-slate-600">
                      <span className="rounded-full bg-white px-2.5 py-1 shadow-[0_6px_18px_rgba(15,23,42,0.05)]">{effectiveRunLabel}</span>
                      <span className="rounded-full bg-white px-2.5 py-1 shadow-[0_6px_18px_rgba(15,23,42,0.05)]">{latestRunSelectionSummary}</span>
                      {elapsedLabel && <span className="rounded-full bg-white px-2.5 py-1 shadow-[0_6px_18px_rgba(15,23,42,0.05)]">Elapsed {elapsedLabel}</span>}
                    </div>
                    <button
                      type="button"
                      onClick={() => void handleCancelRun()}
                      disabled={cancelingRun}
                      className="mt-4 inline-flex w-full items-center justify-center gap-2 rounded-full border border-slate-200 bg-white px-5 py-3 text-sm font-semibold text-slate-800 transition hover:border-rose-200 hover:text-rose-700 disabled:cursor-not-allowed disabled:opacity-55"
                    >
                      {cancelingRun ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <X className="h-4 w-4" />}
                      {cancelingRun ? 'Canceling…' : 'Cancel run'}
                    </button>
                  </div>
                )}

                {loadError && <div className="mt-4 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{loadError}</div>}

                {job?.status !== 'running' && (
                  <button
                    type="submit"
                    disabled={runLocked || !hasSelection || nothingNewToRun}
                    className="mt-4 inline-flex w-full items-center justify-center gap-2 rounded-full bg-[linear-gradient(180deg,#0f5a4f,#004d40)] px-5 py-4 text-sm font-semibold text-white transition hover:brightness-[1.03] disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {runLocked ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                    {runLocked ? 'Running extraction' : nothingNewToRun ? 'Nothing new to run' : 'Run extraction'}
                  </button>
                )}
              </WorkflowSection>

              <WorkflowSection
                step="04"
                title="Inspect"
                detail={
                  job?.status === 'running'
                    ? 'Waiting'
                    : outputUpToDate
                      ? 'Ready'
                      : hasCurrentOutputs
                        ? 'Previous output'
                        : 'Waiting'
                }
                state={outputUpToDate ? 'done' : 'pending'}
                open={expandedWorkflowStep === 4}
                badge={job?.status === 'failed' || job?.status === 'canceled' ? <StatusBadge status={status} /> : undefined}
                onToggle={() => setExpandedWorkflowStep((current) => (current === 4 ? 0 : 4))}
              >
                {job?.status !== 'running' && hasCurrentOutputs && (
                  <div>
                    <div className="flex flex-wrap gap-2">
                      {latestOutputMetaItems.map((item) => (
                        <span
                          key={item}
                          className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-xs font-semibold text-slate-600"
                        >
                          {item}
                        </span>
                        ))}
                    </div>
                  </div>
                )}

                <div className="mt-4">
                  <div className="grid gap-2.5">
                    {hasCurrentOutputs ? (
                      <button
                        type="button"
                        onClick={() =>
                        openArtifactViewer({
                          title: 'Output',
                          subtitle: latestOutputSubtitle,
                          sourceJobId: jobId,
                          runId: latestWholeDocumentRun?.run_id ?? latestCompletedEntry?.run_id ?? job?.run_id ?? null,
                          pageNumbers: latestOutputPages,
                          pageRunIds: mergedDocumentPageRunIds,
                          initialPage: latestOutputPages[0] ?? 1,
                          markdownHref: apiUrl(`/api/jobs/${jobId}/merged-artifact/document.md`),
                          jsonHref: apiUrl(`/api/jobs/${jobId}/merged-artifact/document_ir.json`),
                          initialTab: 'markdown',
                          allowRepair: true,
                        })
                      }
                        className="flex items-center justify-between rounded-2xl border border-slate-200 bg-white px-4 py-3 text-left transition hover:border-[color:var(--theme-primary)]/25 hover:shadow-[0_12px_28px_rgba(0,77,64,0.08)]"
                      >
                        <span>
                          <span className="block text-sm font-medium text-slate-800">Open output</span>
                          {openOutputHint && <span className="mt-0.5 block text-xs text-slate-500">{openOutputHint}</span>}
                        </span>
                        <ChevronRight className="h-4 w-4 text-slate-400" />
                      </button>
                    ) : (
                      <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-4 py-6 text-sm text-slate-500">
                        No output yet.
                      </div>
                    )}

                  </div>
                </div>

                {showCurrentRuns && (
                  <div className="mt-4 border-t border-slate-200/80 pt-4">
                    <div className="overflow-hidden rounded-[18px] border border-slate-200 bg-slate-50/60">
                      <button
                        type="button"
                        onClick={() => toggleHistoryVersion(CURRENT_HISTORY_KEY)}
                        className={cn(
                          'flex w-full items-center justify-between gap-3 px-3.5 py-3 text-left transition hover:bg-white/45',
                          currentRunsExpanded && 'border-b border-slate-200/90 bg-white/45',
                        )}
                      >
                        <span className="text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">Runs</span>
                        <span className="flex items-center gap-2">
                          {historyLoading && <LoaderCircle className="h-4 w-4 animate-spin text-[color:var(--theme-primary)]" />}
                          {currentFileExtractionHistory.length > 1 && (
                            <span className="rounded-full bg-white px-2 py-0.5 text-[11px] font-semibold text-slate-500 ring-1 ring-slate-200">
                              {currentFileExtractionHistory.length}
                            </span>
                          )}
                          <ChevronRight
                            className={cn(
                              'h-4 w-4 text-slate-400 transition-transform',
                              currentRunsExpanded ? 'rotate-90' : '',
                            )}
                          />
                        </span>
                      </button>

                      {currentRunsExpanded && (
                        <div className="grid gap-2 px-3 pb-3 pt-2.5">
                          {currentFileExtractionHistory.map((entry) => {
                            const runTimestamp = formatTimestampLabel(entry.finished_at ?? entry.started_at)
                            const runDuration = formatDurationLabel(entry.duration_sec)
                            const modeLabel = getRunModeLabel(entry.run_mode)

                            return (
                              <div
                                key={entry.run_id ?? `${entry.started_at ?? 'run'}-${entry.run_mode ?? 'unknown'}`}
                                className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 rounded-[14px] border border-slate-200/90 bg-white px-3.5 py-3"
                              >
                                <div className="min-w-0 flex-1">
                                  <div className="flex min-w-0 items-center gap-2">
                                    <span className="shrink-0 rounded-full bg-slate-50 px-2 py-0.5 text-[11px] font-semibold text-slate-700 ring-1 ring-slate-200">
                                      {modeLabel}
                                    </span>
                                    <div className="min-w-0 truncate text-sm font-semibold text-slate-900" title={describeHistorySelection(entry)}>
                                      {compactHistorySelection(entry)}
                                    </div>
                                  </div>
                                  <div className="mt-1 truncate text-xs text-slate-500">
                                    {[runTimestamp, runDuration].filter(Boolean).join(' · ') || 'No timing yet'}
                                  </div>
                                </div>

                                <button
                                  type="button"
                                  onClick={() => openRunHistoryViewer(jobId ?? session.job_id, entry, session.page_count)}
                                  aria-label={`Open ${describeHistorySelection(entry)}`}
                                  title="Open"
                                  className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-slate-200/80 bg-slate-50 text-slate-500 transition hover:border-[color:var(--theme-primary)]/20 hover:text-[color:var(--theme-primary)]"
                                >
                                  <FileText className="h-3.5 w-3.5 text-[color:var(--theme-primary)]" />
                                </button>
                              </div>
                            )
                          })}
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </WorkflowSection>
            </div>
          </form>
          {hasPreviousFiles && (
            <section className="min-w-0 rounded-[30px] border border-slate-300/80 bg-white/88 p-4 shadow-[0_14px_32px_rgba(15,23,42,0.05)] ring-1 ring-white/60 backdrop-blur">
              <div className="flex items-center justify-between gap-3">
                <div className="text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">Previous files</div>
                {historyLoading && <LoaderCircle className="h-4 w-4 animate-spin text-[color:var(--theme-primary)]" />}
              </div>
              <div className="mt-3 grid gap-2.5">
                {previousFileVersions.map((version) => {
                  const versionRuns = version.runs.filter(
                    (entry) => entry.status === 'completed' && runHistoryHasArtifacts(entry),
                  )
                  const versionExpanded = expandedHistoryVersions.includes(version.job_id)
                  const versionCreatedAt = formatTimestampLabel(version.created_at)
                  const versionMeta = [versionCreatedAt, version.page_count ? formatPageCountLabel(version.page_count) : null]
                    .filter(Boolean)
                    .join(' · ')

                  return (
                    <div
                      key={version.job_id}
                      className="overflow-hidden rounded-[18px] border border-slate-200 bg-slate-50/55 px-3.5 py-3.5"
                    >
                      <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-3">
                        <div className="min-w-0 flex-1">
                          <div className="text-sm font-semibold text-slate-900 [overflow-wrap:anywhere]">{version.filename}</div>
                          <div className="mt-1 text-xs text-slate-500">{versionMeta || 'No details yet'}</div>
                        </div>

                        {version.has_output ? (
                          <button
                            type="button"
                            onClick={() => openFileVersionLatestViewer(version)}
                            aria-label={`Open output for ${version.filename}`}
                            title="Open output"
                            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-slate-200/75 bg-white/75 text-slate-500 transition hover:border-[color:var(--theme-primary)]/20 hover:text-[color:var(--theme-primary)]"
                          >
                            <FileText className="h-3.5 w-3.5 text-[color:var(--theme-primary)]" />
                          </button>
                        ) : (
                          <span className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-500">
                            No output
                          </span>
                        )}
                      </div>

                      {versionRuns.length > 0 && (
                        <div className="mt-3">
                          <div className="overflow-hidden rounded-[16px] border border-slate-200 bg-white">
                            <button
                              type="button"
                              onClick={() => toggleHistoryVersion(version.job_id)}
                              className={cn(
                                'flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left transition hover:bg-slate-50/90',
                                versionExpanded && 'border-b border-slate-200/90 bg-slate-50/90',
                              )}
                            >
                              <span className="text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">Runs</span>
                              <span className="flex items-center gap-2">
                                {versionRuns.length > 1 && (
                                  <span className="rounded-full bg-slate-50 px-2 py-0.5 text-[11px] font-semibold text-slate-500 ring-1 ring-slate-200">
                                    {versionRuns.length}
                                  </span>
                                )}
                                <ChevronRight
                                  className={cn(
                                    'h-4 w-4 text-slate-400 transition-transform',
                                    versionExpanded ? 'rotate-90' : '',
                                  )}
                                />
                              </span>
                            </button>

                            {versionExpanded && (
                              <div className="grid gap-2 px-3 pb-3 pt-2.5">
                                {versionRuns.map((entry) => {
                                  const runTimestamp = formatTimestampLabel(entry.finished_at ?? entry.started_at)
                                  const runDuration = formatDurationLabel(entry.duration_sec)
                                  const modeLabel = getRunModeLabel(entry.run_mode)

                                  return (
                                    <div
                                      key={entry.run_id ?? `${version.job_id}-${entry.started_at ?? 'run'}-${entry.run_mode ?? 'unknown'}`}
                                      className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 rounded-[14px] border border-slate-200/90 bg-slate-50/45 px-3.5 py-3"
                                    >
                                      <div className="min-w-0 flex-1">
                                        <div className="flex min-w-0 items-center gap-2">
                                          <span className="shrink-0 rounded-full bg-white px-2 py-0.5 text-[11px] font-semibold text-slate-700 ring-1 ring-slate-200">
                                            {modeLabel}
                                          </span>
                                          <div className="min-w-0 truncate text-sm font-semibold text-slate-900" title={describeHistorySelection(entry)}>
                                            {compactHistorySelection(entry)}
                                          </div>
                                        </div>
                                        <div className="mt-1 truncate text-xs text-slate-500">
                                          {[runTimestamp, runDuration].filter(Boolean).join(' · ') || 'No timing yet'}
                                        </div>
                                      </div>
                                      <button
                                        type="button"
                                        onClick={() => openRunHistoryViewer(version.job_id, entry, version.page_count ?? 0)}
                                        aria-label={`Open ${describeHistorySelection(entry)}`}
                                        title="Open"
                                        className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-slate-200/80 bg-white text-slate-500 transition hover:border-[color:var(--theme-primary)]/20 hover:text-[color:var(--theme-primary)]"
                                      >
                                        <FileText className="h-3.5 w-3.5 text-[color:var(--theme-primary)]" />
                                      </button>
                                    </div>
                                  )
                                })}
                              </div>
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </section>
          )}
          </aside>

          <main className="min-w-0 space-y-4">
            <section className="rounded-[30px] border border-slate-300/80 bg-white/92 px-5 py-4 text-slate-900 shadow-[0_14px_32px_rgba(15,23,42,0.06)] ring-1 ring-white/60 backdrop-blur">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="min-w-0">
                  <h1 className="max-w-3xl break-words text-[1.28rem] font-semibold leading-[1.08] tracking-[-0.03em] text-slate-950 md:text-[1.45rem]">
                    {session.input_pdf_name}
                  </h1>
                </div>
                <div className="flex items-center gap-3">
                  {(status === 'running' || status === 'failed' || status === 'canceled') && <StatusBadge status={status} />}
                </div>
              </div>

              <div className="mt-4 flex flex-wrap gap-2.5">
                {[
                  selectionSummary,
                ]
                  .filter(Boolean)
                  .map((value) => (
                  <span
                    key={String(value)}
                    className="rounded-full border border-slate-200 bg-white/88 px-3.5 py-2 text-xs font-semibold text-slate-700 shadow-[0_10px_22px_rgba(15,23,42,0.04)]"
                  >
                    {value}
                  </span>
                  ))}
              </div>
            </section>

            <section className="rounded-[30px] border border-slate-300/80 bg-white/92 p-4 shadow-[0_14px_32px_rgba(15,23,42,0.06)] ring-1 ring-white/60 backdrop-blur">
              <div className="flex min-h-[44px] flex-wrap items-center justify-between gap-3 px-1 pb-2 pt-1">
                <div className="text-[11px] font-bold uppercase tracking-[0.18em] text-slate-500">
                  Pages
                </div>
                <div className="flex flex-wrap items-center gap-2 self-center">
                  {showSelectedCount && (
                    <span className="inline-flex h-9 items-center rounded-full border border-slate-200 bg-white px-3 text-xs font-semibold text-slate-600 shadow-[0_8px_18px_rgba(15,23,42,0.03)]">
                      {runnableFastSelectionPages.length} selected
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => {
                      const pages = fastSelectablePages
                      setSelectionMode('all')
                      setSelectedPages(pages)
                      setSelectedOutlineIds([])
                      setPageInput(compressPages(pages))
                    }}
                    disabled={runLocked}
                    className="inline-flex h-9 shrink-0 items-center justify-center rounded-full border border-slate-200 bg-white px-4 text-xs font-semibold text-slate-700 shadow-[0_8px_18px_rgba(15,23,42,0.04)] transition hover:border-[color:var(--theme-primary)]/25 hover:text-[color:var(--theme-primary)] disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Select all
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setSelectionMode('pagerange')
                      setSelectedPages([])
                      setSelectedOutlineIds([])
                      setPageInput('')
                    }}
                    disabled={runLocked}
                    className="inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-full border border-slate-200 bg-white px-4 text-xs font-semibold text-slate-700 shadow-[0_8px_18px_rgba(15,23,42,0.04)] transition hover:border-slate-300 hover:text-slate-900 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <RefreshCw className="h-3.5 w-3.5" />
                    Clear
                  </button>
                </div>
              </div>

              <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))' }}>
                {session.pages.map((page) => {
                  const pageNumber = page.page_index + 1
                  const selected = selectionMode === 'all' ? fastSelectablePageSet.has(pageNumber) : selectedPageSet.has(pageNumber)
                  const fastAlreadyExtracted = fastExtractedPageSet.has(pageNumber)
                  return (
                    <article
                      key={page.page_index}
                      className={cn(
                        'group overflow-hidden rounded-[26px] border bg-white shadow-[0_12px_32px_rgba(15,23,42,0.06)] transition-all',
                        selected
                          ? 'border-[color:var(--theme-primary)]/30 shadow-[0_18px_40px_rgba(0,77,64,0.10)]'
                          : 'border-slate-200 hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-[0_18px_36px_rgba(15,23,42,0.08)]',
                      )}
                    >
                      <button
                        type="button"
                        onClick={() => openPageCompare(pageNumber)}
                        disabled={runLocked}
                        className="relative w-full overflow-hidden bg-slate-100 disabled:cursor-not-allowed"
                      >
                        <img
                          src={`/api/jobs/${jobId}/thumb/page_${String(pageNumber).padStart(4, '0')}.jpg`}
                          alt={`Page ${pageNumber}`}
                          className="aspect-[0.7] w-full object-cover transition duration-300 group-hover:scale-[1.015]"
                        />
                        <div className="pointer-events-none absolute inset-0 bg-gradient-to-t from-[rgba(0,77,64,0.68)] via-transparent to-transparent opacity-0 transition group-hover:opacity-100" />
                        <div className="absolute left-3 top-3 inline-flex items-center rounded-full bg-white/92 px-2.5 py-1 text-[11px] font-semibold text-slate-800 shadow-sm">
                          <span>{pageNumber}</span>
                        </div>
                      </button>
                      <div className="space-y-3 px-4 py-3.5">
                        <div>
                          <div className="font-['Iowan_Old_Style','Palatino_Linotype','Book_Antiqua',serif] text-[1.2rem] font-semibold tracking-[-0.03em] text-slate-900">
                            Page {pageNumber}
                          </div>
                        </div>

                        <div>
                          {fastAlreadyExtracted ? (
                            <div
                              title="Already extracted"
                              aria-label="Already extracted"
                              className="inline-flex min-h-[42px] w-full items-center justify-center rounded-full border border-slate-200 bg-slate-100 text-slate-500"
                            >
                              <Check className="h-4 w-4" />
                            </div>
                          ) : (
                            <button
                              type="button"
                              onClick={() => togglePage(pageNumber)}
                              disabled={runLocked}
                              className={cn(
                                'inline-flex min-h-[42px] w-full items-center justify-center rounded-full px-4 py-2 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-60',
                                selected
                                  ? 'border border-[color:var(--theme-primary)]/12 bg-[color:var(--theme-primary)]/[0.06] text-[color:var(--theme-primary)] hover:bg-[color:var(--theme-primary)]/[0.08]'
                                  : 'bg-[color:var(--theme-primary)] text-white hover:brightness-[1.03]',
                              )}
                            >
                              {selected ? (
                                <>
                                  <Check className="mr-2 h-4 w-4" />
                                  Selected
                                </>
                              ) : (
                                'Add'
                              )}
                            </button>
                          )}
                        </div>
                      </div>
                    </article>
                  )
                })}
              </div>
            </section>
          </main>
        </div>
        ) : null}
      </div>

      {artifactViewer && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-[rgba(5,28,23,0.7)] p-4 backdrop-blur-md">
          {(() => {
            const currentArtifactPage = artifactPage ?? artifactViewer.pageNumbers[0] ?? 1
            const artifactJobId = artifactViewer.sourceJobId ?? jobId
            const artifactImageHref = artifactJobId
              ? `/api/jobs/${artifactJobId}/preview/page_${String(currentArtifactPage).padStart(4, '0')}.jpg`
              : ''
            return (
              <div className="grid h-[92vh] w-full max-w-[1680px] overflow-hidden rounded-[30px] border border-white/10 bg-white shadow-[0_30px_100px_rgba(15,23,42,0.3)] lg:grid-cols-[minmax(0,1.08fr)_minmax(520px,0.92fr)]">
                <div className="flex min-h-0 flex-col overflow-hidden border-r border-slate-200 bg-[linear-gradient(180deg,#e2ebe7_0%,#d8e3dd_48%,#ccd8d2_100%)]">
                  <div className="flex items-center justify-between gap-3 border-b border-[#0b3b34]/10 bg-[linear-gradient(180deg,rgba(15,90,79,0.92),rgba(11,59,52,0.88))] px-4 py-3 text-white">
                    <div>
                      <div className="text-[11px] font-bold uppercase tracking-[0.18em] text-white/70">Source page</div>
                      <div className="mt-1 font-['Iowan_Old_Style','Palatino_Linotype','Book_Antiqua',serif] text-2xl font-semibold tracking-[-0.03em]">
                        Page {currentArtifactPage}
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => setArtifactImageRotation((current) => (current + 90) % 360)}
                        className="inline-flex h-10 w-10 items-center justify-center rounded-full border border-white/15 bg-white/5 text-white transition hover:bg-white/10"
                        title="Rotate preview 90°"
                        aria-label="Rotate preview 90 degrees"
                      >
                        <RotateCw className="h-4 w-4" />
                      </button>
                      <button
                        type="button"
                        onClick={() =>
                          artifactHasPrevPage && setArtifactPage(artifactNavigationPages[artifactPageCursor - 1])
                        }
                        disabled={!artifactHasPrevPage}
                        className="inline-flex h-10 w-10 items-center justify-center rounded-full border border-white/15 bg-white/5 text-white transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-35"
                      >
                        <ChevronLeft className="h-4 w-4" />
                      </button>
                      <button
                        type="button"
                        onClick={() =>
                          artifactHasNextPage && setArtifactPage(artifactNavigationPages[artifactPageCursor + 1])
                        }
                        disabled={!artifactHasNextPage}
                        className="inline-flex h-10 w-10 items-center justify-center rounded-full border border-white/15 bg-white/5 text-white transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-35"
                      >
                        <ChevronRight className="h-4 w-4" />
                      </button>
                    </div>
                  </div>

                  <div
                    ref={artifactImageViewportRef}
                    className="min-h-0 flex-1 overflow-auto bg-[radial-gradient(circle_at_top,rgba(255,255,255,0.36),rgba(255,255,255,0)_52%)]"
                  >
                    <div className="flex min-h-full min-w-full items-center justify-center p-4">
                      {artifactImageDisplayBox ? (
                        <div
                          className="relative shrink-0 transition-[width,height] duration-200 ease-out"
                          style={{
                            width: artifactImageDisplayBox.frameWidth,
                            height: artifactImageDisplayBox.frameHeight,
                          }}
                        >
                          <img
                            key={`${artifactImageHref}:${artifactImageRotation}:fit`}
                            src={artifactImageHref}
                            alt={`Source page ${currentArtifactPage}`}
                            onLoad={(event) =>
                              setArtifactImageNaturalSize({
                                width: event.currentTarget.naturalWidth,
                                height: event.currentTarget.naturalHeight,
                              })
                            }
                            className="absolute left-1/2 top-1/2 block object-contain transition-transform duration-200 ease-out"
                            style={{
                              width: artifactImageDisplayBox.imageWidth,
                              height: artifactImageDisplayBox.imageHeight,
                              maxWidth: 'none',
                              maxHeight: 'none',
                              transform: `translate(-50%, -50%) rotate(${artifactImageRotation}deg)`,
                              transformOrigin: 'center center',
                            }}
                          />
                        </div>
                      ) : (
                        <img
                          key={`${artifactImageHref}:${artifactImageRotation}:fallback`}
                          src={artifactImageHref}
                          alt={`Source page ${currentArtifactPage}`}
                          onLoad={(event) =>
                            setArtifactImageNaturalSize({
                              width: event.currentTarget.naturalWidth,
                              height: event.currentTarget.naturalHeight,
                            })
                          }
                          className="block max-h-full max-w-full object-contain transition-transform duration-200 ease-out"
                          style={{
                            transform: `rotate(${artifactImageRotation}deg)`,
                            transformOrigin: 'center center',
                          }}
                        />
                      )}
                    </div>
                  </div>
                </div>

                <aside className="flex min-h-0 flex-col overflow-hidden p-5">
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0">
                      <div className="text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">Output</div>
                      <div className="mt-1 font-['Iowan_Old_Style','Palatino_Linotype','Book_Antiqua',serif] text-[1.65rem] font-semibold tracking-[-0.03em] text-slate-900">
                        Page {currentArtifactPage}
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      {artifactRepairAllowed && currentArtifactPage !== null && artifactPageHasOutput && (
                        <button
                          type="button"
                          onClick={() => void handleRepairPage(currentArtifactPage)}
                          disabled={
                            artifactCurrentPageRepairing ||
                            artifactCurrentPageRepairDone ||
                            artifactPageAlreadyRepaired ||
                            (artifactRepairInProgress && !artifactCurrentPageRepairing) ||
                            (runLocked && !artifactCurrentPageRepairing)
                          }
                          className={cn(
                            'relative inline-flex items-center gap-2 overflow-hidden rounded-full border px-3 py-2 text-sm font-semibold transition disabled:cursor-not-allowed',
                            artifactCurrentPageRepairing
                              ? 'border-[color:var(--theme-primary)] bg-[color:var(--theme-primary)] text-white shadow-[0_16px_34px_rgba(0,77,64,0.24)]'
                              : artifactCurrentPageRepairDone || artifactPageAlreadyRepaired
                                ? 'border-[color:var(--theme-primary)]/12 bg-[color:var(--theme-primary)]/[0.04] text-[color:var(--theme-primary)] opacity-70'
                                : 'border-[color:var(--theme-primary)]/16 bg-[color:var(--theme-primary)]/[0.04] text-[color:var(--theme-primary)] hover:border-[color:var(--theme-primary)]/28 hover:bg-[color:var(--theme-primary)]/[0.06]',
                            (runLocked || artifactRepairInProgress) &&
                              !artifactCurrentPageRepairing &&
                              !artifactCurrentPageRepairDone &&
                              !artifactPageAlreadyRepaired
                              ? 'opacity-45'
                              : '',
                          )}
                        >
                          {artifactCurrentPageRepairing && (
                            <>
                              <span aria-hidden className="repair-progress-track" />
                              <span aria-hidden className="repair-progress-indicator" />
                            </>
                          )}
                          {artifactCurrentPageRepairing ? (
                            <LoaderCircle className="relative z-[1] h-4 w-4 animate-spin" />
                          ) : artifactCurrentPageRepairDone || artifactPageAlreadyRepaired ? (
                            <Check className="relative z-[1] h-4 w-4" />
                          ) : (
                            <Play className="relative z-[1] h-4 w-4" />
                          )}
                          <span className="relative z-[1]">{artifactRepairButtonLabel}</span>
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => setArtifactViewer(null)}
                        className="rounded-full border border-slate-200 p-2 text-slate-500 transition hover:border-slate-300 hover:text-slate-800"
                      >
                        <X className="h-4 w-4" />
                      </button>
                    </div>
                  </div>

                  {repairActionError && (
                    <div className="mt-3 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
                      {repairActionError}
                    </div>
                  )}

                  {currentViewerRepairTask?.phase === 'error' && artifactRepairStatusLabel && (
                    <div
                      className={cn(
                        'mt-3 overflow-hidden rounded-[20px] border px-4 py-3 text-sm shadow-[0_14px_34px_rgba(15,23,42,0.06)]',
                        'border-rose-200 bg-rose-50 text-rose-700',
                      )}
                    >
                      <div className="flex items-start gap-3">
                        <div className="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-rose-100 text-rose-700">
                          <X className="h-4 w-4" />
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="font-semibold">{artifactRepairStatusLabel}</div>
                          {artifactRepairStatusBody && (
                            <div className="mt-0.5 text-xs leading-5 opacity-[0.78]">{artifactRepairStatusBody}</div>
                          )}
                        </div>
                      </div>
                    </div>
                  )}

                  {artifactSourcePages.length > 1 && (
                    <div className="mt-4 rounded-[22px] border border-slate-200 bg-slate-50/80 px-4 py-3">
                      <div className="flex items-center justify-between gap-3">
                        <div className="text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">Pages</div>
                        <div className="text-xs font-medium text-slate-500">Use arrow keys or click a page</div>
                      </div>
                      <div ref={artifactThumbStripRef} className="mt-3 flex gap-2.5 overflow-x-auto pb-1">
                        {artifactSourcePages.map((pageNumber) => (
                          <button
                            key={pageNumber}
                            data-artifact-page={pageNumber}
                            type="button"
                            onClick={() => setArtifactPage(pageNumber)}
                            className={cn(
                              'group shrink-0 overflow-hidden rounded-[18px] border bg-white text-left transition',
                              pageNumber === currentArtifactPage
                                ? 'border-[color:var(--theme-primary)] shadow-[0_14px_28px_rgba(0,77,64,0.16)]'
                                : 'border-slate-200 hover:border-[color:var(--theme-primary)]/25 hover:shadow-[0_10px_22px_rgba(15,23,42,0.08)]',
                            )}
                          >
                            <div className="relative w-[88px]">
                              <img
                                src={
                                  artifactJobId
                                    ? `/api/jobs/${artifactJobId}/thumb/page_${String(pageNumber).padStart(4, '0')}.jpg`
                                    : ''
                                }
                                alt={`Page ${pageNumber}`}
                                className="aspect-[0.72] w-full object-cover transition duration-300 group-hover:scale-[1.02]"
                              />
                              <div className="pointer-events-none absolute inset-x-0 bottom-0 h-12 bg-gradient-to-t from-[rgba(15,23,42,0.72)] to-transparent" />
                              <div
                                className={cn(
                                  'absolute left-2 top-2 inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-[10px] font-bold shadow-sm',
                                  pageNumber === currentArtifactPage
                                    ? 'bg-[color:var(--theme-primary)] text-white'
                                    : 'bg-white/92 text-slate-700',
                                )}
                              >
                                <span>{pageNumber}</span>
                              </div>
                              <div className="absolute inset-x-0 bottom-0 px-2.5 py-2 text-[11px] font-semibold text-white">
                                Page {pageNumber}
                              </div>
                            </div>
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  <div className="mt-5 flex min-h-0 flex-1 flex-col justify-end">
                    <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-[24px] border border-slate-200 bg-slate-50 p-4">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div className="inline-flex flex-wrap items-center gap-1 rounded-full border border-slate-200 bg-white p-1 shadow-[0_10px_22px_rgba(15,23,42,0.04)]">
                          {artifactHasMarkdown && (
                            <button
                              type="button"
                              onClick={() => setArtifactViewMode('markdown')}
                              className={cn(
                                'inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-semibold transition',
                                artifactViewMode === 'markdown'
                                  ? 'bg-[color:var(--theme-primary)] text-white'
                                  : 'text-slate-600 hover:text-[color:var(--theme-primary)]',
                              )}
                            >
                              Markdown
                            </button>
                          )}
                          {artifactHasJson && (
                            <button
                              type="button"
                              onClick={() => setArtifactViewMode('json')}
                              className={cn(
                                'inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-semibold transition',
                                artifactViewMode === 'json'
                                  ? 'bg-[color:var(--theme-primary)] text-white'
                                  : 'text-slate-600 hover:text-[color:var(--theme-primary)]',
                              )}
                            >
                              JSON
                            </button>
                          )}
                        </div>

                        <div className="flex items-center gap-2">
                          {artifactHasImageAgentAction && (
                            <button
                              type="button"
                              onClick={() => openArtifactImageAgentPanel()}
                              className="inline-flex items-center gap-2 rounded-full border border-[color:var(--theme-primary)]/18 bg-[color:var(--theme-primary)]/[0.05] px-3 py-1.5 text-xs font-semibold text-[color:var(--theme-primary)] transition hover:border-[color:var(--theme-primary)]/30 hover:bg-[color:var(--theme-primary)]/[0.08]"
                            >
                              {artifactImageAgentLoading ? (
                                <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
                              ) : (
                                <Brain className="h-3.5 w-3.5" />
                              )}
                              Image Agent
                            </button>
                          )}
                          {artifactDownloadHref && (
                            <a
                              href={artifactDownloadHref}
                              download
                              className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition hover:border-[color:var(--theme-primary)]/25 hover:text-[color:var(--theme-primary)]"
                            >
                              <Download className="h-3.5 w-3.5" />
                              Download output
                            </a>
                          )}
                          <button
                            type="button"
                            onClick={() => void copyToClipboard(artifactCopyText, 'artifact-viewer')}
                            disabled={!artifactCopyText.trim()}
                            className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition hover:border-[color:var(--theme-primary)]/25 hover:text-[color:var(--theme-primary)] disabled:cursor-not-allowed disabled:opacity-45"
                          >
                            <Copy className="h-3.5 w-3.5" />
                            {copiedSurface === 'artifact-viewer' ? 'Copied' : 'Copy'}
                          </button>
                        </div>
                      </div>

                      {artifactPreviewError && <div className="mt-4 text-sm text-rose-600">{artifactPreviewError}</div>}
                      {!artifactPreviewError && artifactLoading && (
                        <div className="mt-4 inline-flex items-center gap-2 text-sm text-slate-500">
                          <LoaderCircle className="h-4 w-4 animate-spin text-[color:var(--theme-primary)]" />
                          {artifactCurrentPageRepairing ? 'Refreshing repaired output' : 'Loading page output'}
                        </div>
                      )}

                      {artifactPreviewData && !artifactLoading && (
                        <div className="relative mt-4 min-h-0 flex-1 overflow-hidden rounded-[22px] bg-white/94 shadow-[inset_0_1px_0_rgba(255,255,255,0.65)]">
                          <div
                            className={cn(
                              'flex h-full min-h-0 flex-col px-5 py-4 transition',
                              artifactImageAgentPanelOpen ? 'scale-[0.995] opacity-40 blur-[1px]' : '',
                            )}
                          >
                            <div className="mb-4 flex items-center justify-between gap-3">
                              <div className="text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">
                                {artifactViewMode === 'json' ? 'JSON' : 'Markdown'}
                              </div>
                            </div>

                            <div className="min-h-0 flex-1 overflow-y-auto">
                              {artifactViewMode === 'markdown' ? (
                                <div className="markdown-surface text-[15px] leading-7">
                                  <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeRaw, rehypeKatex]}>
                                    {artifactPreviewData.page_markdown?.trim() || '_No extracted content on this page._'}
                                  </ReactMarkdown>
                                </div>
                              ) : artifactPreviewJsonText ? (
                                <pre className="overflow-auto rounded-[20px] bg-slate-50 p-4 font-mono text-[12px] leading-6 text-slate-800">
                                  {artifactPreviewJsonText}
                                </pre>
                              ) : (
                                <div className="text-sm text-slate-500">No page-level JSON for this page.</div>
                              )}
                            </div>
                          </div>

                          {artifactImageAgentPanelOpen && (
                            <div className="absolute inset-3 z-10 flex min-h-0 flex-col overflow-hidden rounded-[20px] border border-[color:var(--theme-secondary)]/34 bg-white shadow-[0_22px_50px_rgba(15,23,42,0.18)]">
                              <div className="flex items-center justify-between gap-3 border-b border-slate-200 px-4 py-3">
                                <div className="flex min-w-0 items-center gap-3">
                                  <ImageAgentMark compact />
                                  <div className="min-w-0 text-sm font-semibold text-slate-900">Image Agent</div>
                                </div>
                                <button
                                  type="button"
                                  onClick={() => setArtifactImageAgentPanelOpen(false)}
                                  className="rounded-full border border-slate-200 p-2 text-slate-500 transition hover:border-slate-300 hover:text-slate-800"
                                >
                                  <X className="h-4 w-4" />
                                </button>
                              </div>

                              <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
                                {artifactImageAgentLoading ? (
                                  <div className="flex h-full min-h-[260px] flex-col items-center justify-center gap-3 text-center text-slate-500">
                                    <LoaderCircle className="h-5 w-5 animate-spin text-[color:var(--theme-primary)]" />
                                    <div className="text-sm font-medium text-slate-700">Generating AI reading</div>
                                  </div>
                                ) : artifactImageAgentError ? (
                                  <div className="rounded-[18px] border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
                                    {artifactImageAgentError}
                                  </div>
                                ) : artifactImageAgent?.interpretationMarkdown || artifactImageAgent?.altText ? (
                                  <div className="markdown-surface text-[15px] leading-7">
                                    <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeRaw, rehypeKatex]}>
                                      {artifactImageAgent?.interpretationMarkdown || artifactImageAgent?.altText || ''}
                                    </ReactMarkdown>
                                  </div>
                                ) : artifactPreviewData?.image_agent_empty ? (
                                  <div className="rounded-[18px] border border-slate-200 bg-slate-50 px-4 py-3 text-sm leading-7 text-slate-700">
                                    Image checked. No extra structure found.
                                  </div>
                                ) : artifactCanGenerateImageAgent ? (
                                  <button
                                    type="button"
                                    onClick={() => artifactPage !== null && void handleGenerateImageAgent(artifactPage)}
                                    disabled={artifactImageAgentLoading || artifactPage === null}
                                    className="flex w-full items-center justify-between gap-3 rounded-[18px] border border-slate-200 bg-slate-50/80 px-4 py-4 text-left transition hover:border-[color:var(--theme-primary)]/22 hover:bg-white disabled:cursor-not-allowed disabled:opacity-55"
                                  >
                                    <div className="flex min-w-0 items-center gap-3">
                                      <div className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-[color:var(--theme-primary)]/12 bg-[color:var(--theme-primary)]/[0.05] text-[color:var(--theme-primary)]">
                                        <Brain className="h-4 w-4" />
                                      </div>
                                      <div className="min-w-0 text-sm font-semibold text-slate-900">Generate AI reading</div>
                                    </div>
                                    <ChevronRight className="h-4 w-4 shrink-0 text-slate-400" />
                                  </button>
                                ) : (
                                  <div className="text-sm text-slate-500">Not available on this page.</div>
                                )}
                              </div>
                            </div>
                          )}

                        </div>
                      )}
                    </div>
                  </div>
                </aside>
              </div>
            )
          })()}
        </div>
      )}
    </div>
  )
}





