export type SelectionMode = 'all' | 'outline' | 'pagerange'
export type RunMode = 'fast' | 'reliable'

export type ImageAgentSnapshot = {
  enabled: boolean
  name?: string | null
  model?: string | null
  image_pages_detected?: number | null
  image_pages_enriched?: number | null
  image_pages_failed?: number | null
}

export type SessionPage = {
  page_index: number
}

export type OutlineItem = {
  id: number
  title: string
  page_index: number
  level: number
}

export type JobSnapshot = {
  job_id?: string
  run_id?: string | null
  status: 'preparing' | 'ready' | 'running' | 'completed' | 'failed' | 'canceled'
  message: string
  stage: string
  progress_percent: number
  output_dir: string
  selection_mode?: string | null
  selection?: string | null
  run_mode?: RunMode | null
  artifacts: Record<string, string>
  log_tail?: string | null
  created_at?: string | null
  started_at?: string | null
  finished_at?: string | null
  duration_sec?: number | null
  cancel_requested?: boolean
  cascade_attempt?: number | null
  failed_pages_count?: number | null
  image_agent?: ImageAgentSnapshot | null
  engines?: {
    primary?: string
    fallback?: string
  }
}

export type SessionPayload = {
  job_id: string
  document_id: string
  file_version: number
  replaces_job_id?: string | null
  input_pdf: string
  input_pdf_name: string
  job_dir: string
  page_count: number
  has_outline: boolean
  default_selection_mode: SelectionMode
  default_output_dir: string
  pages: SessionPage[]
  outline: OutlineItem[]
  job: JobSnapshot
}

export type PagePreview = {
  run_id?: string | null
  page_number: number
  page_index: number
  in_document_ir: boolean
  block_count: number | null
  block_types: Record<string, number>
  source_engine?: string
  page_markdown?: string
  page_ir?: Record<string, unknown> | null
  image_content_detected?: boolean
  image_hint?: string | null
  image_alt_text?: string | null
  image_interpretation_markdown?: string | null
  image_agent_language?: string | null
  image_agent_kind?: string | null
  image_agent_generated?: boolean
  image_agent_empty?: boolean
}

export type RunHistoryEntry = {
  job_id: string
  document_id?: string
  file_version?: number
  replaces_job_id?: string | null
  run_id?: string | null
  filename?: string | null
  page_count?: number | null
  status: JobSnapshot['status']
  run_mode?: RunMode | null
  selection_mode?: string | null
  selection?: string | null
  resolved_pages?: number[]
  started_at?: string | null
  finished_at?: string | null
  duration_sec?: number | null
  failed_pages_count?: number | null
  cascade_attempt?: number | null
  image_agent?: ImageAgentSnapshot | null
  engine_config?: string | null
  repair_engine_version?: string | null
  artifact_urls: Record<string, string>
}

export type RunHistoryPayload = {
  runs: RunHistoryEntry[]
}

export type FileVersionHistoryEntry = {
  job_id: string
  document_id: string
  file_version: number
  replaces_job_id?: string | null
  filename: string
  created_at?: string | null
  page_count?: number | null
  is_current?: boolean
  has_output?: boolean
  latest_output_pages: number[]
  effective_page_run_ids: Record<number, string | null | undefined>
  merged_artifact_urls: Record<string, string>
  runs: RunHistoryEntry[]
}

export type FileHistoryPayload = {
  document_id: string
  current_job_id: string
  versions: FileVersionHistoryEntry[]
}

export type UploadResponse = {
  job_id: string
  session: SessionPayload
}
