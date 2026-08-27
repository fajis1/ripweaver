import { Fragment, useCallback, useEffect, useRef, useState, type FormEvent } from 'react';

interface PreviewDrive {
  disc_id: string;
  drive_index: number;
  strategy: 'single-open' | 'per-title';
  title_count: number;
  estimated_bytes: number;
  minimum_length_seconds: number | null;
  reason: string;
  selection_mode: 'episode' | 'bonus-features' | 'mixed' | 'automatic-bonus-fallback' | 'reviewed-special-features';
  metadata_source?: 'thediscdb' | 'ripweaver-catalogue' | null;
  metadata_status?: 'matched' | 'not-found' | 'title-mismatch' | 'unavailable' | 'support-required' | null;
  metadata_matched_title_count?: number;
}

interface PreviewJob {
  job_id: string;
  drive_index: number;
  title_index: number;
  estimated_bytes: number | null;
  staging_destination: string;
  final_destination: string | null;
  collision_status: string;
  display_name?: string | null;
  extras_folder?: string | null;
  identification_status?: 'catalogue-match' | 'disc-database-match' | 'evidence-required' | null;
  prior_outcome_name?: string | null;
  prior_library_relative?: string | null;
  prior_episode_id?: string | null;
  prior_library_status?: 'present' | 'missing' | 'unavailable' | null;
}

interface RipPreview {
  execution_authorized: false;
  plan_sha256: string;
  drives: PreviewDrive[];
  jobs: PreviewJob[];
  skipped_discs: Array<{
    disc_id: string;
    drive_index: number;
    reasons: string[];
  }>;
  skipped_titles: Array<{
    disc_fingerprint: string;
    title_index: number;
    reason: string;
  }>;
  held_titles?: Array<{
    disc_fingerprint: string;
    title_index: number;
    reason: string;
    outcome_name?: string | null;
    library_relative?: string | null;
    episode_id?: string | null;
  }>;
  collision_count: number;
  requires_review: boolean;
  limitations: string[];
}

const allKnownTitlesAlreadyInLibrary = (preview: RipPreview | null | undefined): boolean => Boolean(
  preview
  && preview.jobs.length > 0
  && preview.jobs.every((job) => job.prior_library_status === 'present')
  && (preview.held_titles?.length ?? 0) > 0,
);

const previewHasDiscFingerprint = (
  preview: RipPreview | null | undefined,
  discFingerprint: string | null | undefined,
): boolean => Boolean(
  preview
  && discFingerprint
  && preview.jobs.some((job) => job.staging_destination.split('/').includes(discFingerprint)),
);

const activeRipJobStates = new Set(['authorized', 'queued', 'running', 'pause_requested']);

const dashboardDiscFingerprints = (
  driveDashboard: DriveDashboard | null,
  savedJob: OrchestrationJob | null,
  jobDashboard: JobDashboard | null,
): string[] => {
  const fingerprints = new Set(
    (driveDashboard?.drives ?? [])
      .filter((drive) => drive.has_disc && drive.current_disc_fingerprint)
      .map((drive) => drive.current_disc_fingerprint as string),
  );
  savedJob?.preview?.jobs.forEach((job) => {
    const fingerprint = job.staging_destination.split('/').find((part) => /^[0-9a-f]{16}$/.test(part));
    if (fingerprint) fingerprints.add(fingerprint);
  });
  jobDashboard?.jobs
    .filter((job) => activeRipJobStates.has(job.state))
    .flatMap((job) => job.preview?.jobs ?? [])
    .forEach((job) => {
      const fingerprint = job.staging_destination.split('/').find((part) => /^[0-9a-f]{16}$/.test(part));
      if (fingerprint) fingerprints.add(fingerprint);
    });
  return Array.from(fingerprints).slice(0, 16);
};

const scopedDashboardUrl = (path: string, scope: string, discFingerprints: string[] = []) => {
  const params = new URLSearchParams({ scope });
  if (discFingerprints.length > 0) params.set('disc_fingerprints', discFingerprints.join(','));
  return `${path}?${params.toString()}`;
};

type DashboardJsonResult<T> = { ok: boolean; payload: T | null };
const dashboardJsonRequests = new Map<string, Promise<DashboardJsonResult<unknown>>>();
let lastMatchingPerformanceRefreshAt = 0;

const sharedDashboardJson = <T,>(url: string): Promise<DashboardJsonResult<T>> => {
  const existing = dashboardJsonRequests.get(url);
  if (existing) return existing as Promise<DashboardJsonResult<T>>;
  const request: Promise<DashboardJsonResult<unknown>> = fetch(url)
    .then(async (response) => ({
      ok: response.ok,
      payload: response.ok ? await response.json() as unknown : null,
    }))
    .finally(() => {
      dashboardJsonRequests.delete(url);
    });
  dashboardJsonRequests.set(url, request);
  return request as Promise<DashboardJsonResult<T>>;
};

const dashboardHasActiveWork = (
  driveDashboard: DriveDashboard | null,
  jobDashboard: JobDashboard | null,
  pipelineQueue: PipelineQueue | null,
) => Boolean(
  driveDashboard?.refresh_in_progress
  || driveDashboard?.busy_drive_indexes?.length
  || jobDashboard?.jobs.some((job) => activeRipJobStates.has(job.state))
  || pipelineQueue?.items.some((item) => (
    ['queued', 'running'].includes(item.state)
    || (item.state === 'review_required' && item.review_code?.endsWith('_running'))
  )),
);

const normalizeDiscLabel = (value: string): string => value
  .replace(/[_-]+/g, ' ')
  .replace(/\s+/g, ' ')
  .trim();

const inferDiscSeason = (value: string): number | null => {
  const match = normalizeDiscLabel(value).match(/\b(?:SEASON\s*|S)(\d{1,2})\b/i);
  return match ? Number(match[1]) : null;
};

const inferCanonicalSeriesFromDiscLabel = (value: string): string => normalizeDiscLabel(value)
  .replace(/\bSUPERFAN\s+EPISODES?\s+(?:S|SEASON\s*)\d{1,2}\b.*$/i, '')
  .replace(/\b(?:DVD|DISC|DISK|VOLUME|VOL)\s*\d+\b.*$/i, '')
  .replace(/\bCSR\s+DIM\s*\d+\b.*$/i, '')
  .replace(/\b(?:SEASON\s*|S)\d{1,2}\b.*$/i, '')
  .replace(/\s+\d+\s*$/i, '')
  .replace(/\s+/g, ' ')
  .trim();

const episodeSequenceReviewCodes = new Set([
  'missing_season_context',
  'episode_match_review',
  'unmatched_disc_analysis_required',
  'all_season_analysis_failed',
  'all_season_series_not_found',
  'all_season_evidence_failed',
  'all_season_catalog_unavailable',
  'all_season_sequence_review_required',
  'independent_episode_evidence_required',
  'whole_disc_coherence_review_required',
]);

const tvEpisodeProviderReviewCodes = new Set([
  'gemini_descriptive_review_required',
  'gemini_analysis_interrupted',
  'gemini_analysis_failed',
  'gemini_audio_evidence_insufficient',
  'gemini_catalog_unavailable',
  'gemini_provider_failed',
  'gemini_credential_rejected',
  'gemini_rate_limited',
  'gemini_provider_unavailable',
  'gemini_request_rejected',
  'gemini_network_failed',
  'gemini_response_invalid',
]);

interface ExistingRipRecoveryPlan {
  plan_sha256: string;
  candidates: Array<{ job_id: string; title_index: number; basename: string; size_bytes: number; candidate_id: string }>;
  missing_title_indexes: number[];
  ambiguous_title_indexes: number[];
}

interface FailedRipCleanupPlan {
  attempt_directory_count: number;
  file_count: number;
  total_bytes: number;
  plan_sha256: string;
}

interface PreviewRequest {
  report_paths: string[];
  media_contexts: Record<string, {
    series_name: string;
    season: number | null;
    disc_number: number;
  }>;
  output_root: string | null;
}

interface OrchestrationJob {
  job_id: string;
  plan_sha256: string;
  state: string;
  executor_attached: boolean;
  created_at: string;
  updated_at: string;
  preview?: RipPreview;
  error_type?: string | null;
  error_category?: string | null;
  failed_drive_indexes?: number[];
  recommendations?: string[];
  rip_title_summary?: {
    total_titles: number;
    verified_titles: Array<{ job_id: string; title_index: number; drive_index: number }>;
    unfinished_titles: Array<{ job_id: string; title_index: number; drive_index: number }>;
  } | null;
  rip_progress_percent?: number | null;
  rip_transfer_mib_s?: number | null;
  rip_progress_scope?: string | null;
  rip_progress_updated_at?: string | null;
  rip_overall_progress_percent?: number | null;
  rip_completed_title_count?: number | null;
  rip_total_title_count?: number | null;
  rip_activity_status?: string;
  rip_activity_age_seconds?: number | null;
  rip_possibly_stalled?: boolean;
  rip_stall_after_seconds?: number | null;
  pipeline_handoff_status?: string;
  pipeline_handoff_pending_title_count?: number;
  pipeline_queued_title_count?: number;
}

interface JobDashboard {
  automatic_processing_enabled: boolean;
  watcher_attached: boolean;
  jobs: OrchestrationJob[];
}

interface CatalogueSupportStatus {
  enabled: boolean;
  connected: boolean;
  registered: boolean;
  policy: {
    policy_version: string;
    terms_version: string;
    minimum_amount_cents: number;
    minimum_rate_cents: number;
    maximum_rate_cents: number;
    default_rate_cents: number;
    payments_enabled: boolean;
    support_message: string;
    availability_disclosure: string;
    refund_disclosure: string;
  } | null;
  usage: {
    monthly_limit: number;
    monthly_used: number;
    monthly_remaining: number;
    contribution_credits: number;
    purchased_credits: number;
    total_automatic_remaining: number;
  } | null;
}

interface DriveSlot {
  drive_index: number;
  available: boolean;
  has_disc: boolean;
  disc_label: string | null;
  current_job_id?: string | null;
  current_disc_fingerprint?: string | null;
  mapping_id?: string | null;
  display_name?: string | null;
  connection_type?: 'usb' | 'sata' | 'unknown' | null;
  mapping_status?: 'trusted' | 'ignored' | 'unmapped';
  mapping_warning?: 'new_device' | 'possible_identity_change' | 'identity_unavailable' | null;
  prior_similar_mapping_count?: number;
  makemkv_confirmed?: boolean;
}

const completeDiscAutoEjectKey = (drive: DriveSlot, job: OrchestrationJob): string => (
  `${job.job_id}:${drive.current_disc_fingerprint ?? 'unknown-disc'}`
);

interface DriveDashboard {
  watcher_attached: boolean;
  refresh_mode: 'explicit' | 'startup-and-events';
  status: 'not_scanned' | 'ready' | 'error';
  refreshed_at: string | null;
  error_type: string | null;
  error_code?: 'timeout' | 'executable_missing' | 'no_drives' | 'discovery_failed' | null;
  refresh_in_progress?: boolean;
  refresh_deferred?: boolean;
  automatic_discovery_paused?: boolean;
  automatic_discovery_pause_reason?: string | null;
  automatic_discovery_timeout_count?: number;
  busy_drive_indexes?: number[];
  physical_drive_operations?: Record<number, string>;
  mapping_plan_sha256?: string | null;
  mapping_summary?: Record<'trusted' | 'ignored' | 'unmapped', number>;
  retired_mapping_count?: number;
  automatic_processing_requested?: boolean;
  drives: DriveSlot[];
}

type DiscContentType = '' | 'tv' | 'movie' | 'extras' | 'mixed';

interface DiscSetup {
  contentType: DiscContentType;
  handbrakeProfile: string;
  addMissingOnly: boolean;
  automaticProfile: string;
}

interface PublicConfig {
  default_handbrake_profile?: string;
  remember_last_handbrake_profile?: boolean;
  makemkv_path?: string;
  automatic_eject_after_rip?: boolean;
  automatic_gemini_ambiguity_fallback?: boolean;
  retained_source_ttl_days?: number;
  jellyfin_tv_root?: string;
  jellyfin_movie_root?: string;
}

interface StoredProfile {
  profile_id: string;
  display_name: string;
  built_in: boolean;
}

type MatchEvidenceSummary = Record<string, string | number | boolean | null | string[]>;

interface PipelineQueueItem {
  media_id: string;
  artifact_sha256: string;
  disc_fingerprint: string | null;
  title_index: number | null;
  series_name?: string | null;
  display_name: string | null;
  match_summary: string | null;
  catalogue_candidate_help: {
    series_name: string;
    season: number;
    episode: number;
    title: string;
    independent_support: 1;
    automatic: false;
  } | null;
  state: string;
  stage: string;
  updated_at: string;
  location_label: string;
  location_relative: string | null;
  location_root_key: 'jellyfin_tv_root' | 'jellyfin_movie_root' | null;
  output_size_bytes: number | null;
  retained_source_available: boolean;
  original_source_unavailable: boolean;
  staged_source_available: boolean;
  pipeline_media_available: boolean;
  provisional_match: boolean;
  gemini_confidence: number | null;
  gemini_series_proposal: {
    series_name: string;
    series_names: string[];
    confidence: number;
    tmdb_id: number | null;
  } | null;
  error_type: string | null;
  review_code: string | null;
  visual_review_code: string | null;
  likely_removable: boolean;
  activity_status?: string;
  activity_age_seconds?: number | null;
  possibly_stalled?: boolean;
  stall_after_seconds?: number | null;
  identification_attempts?: Array<{
    branch: string;
    disposition: string;
    summary: MatchEvidenceSummary;
  }>;
}

interface PipelineQueue {
  paused: boolean;
  startup_resume_in_seconds?: number | null;
  downstream_worker_limit: number;
  automatic_processing_enabled: boolean;
  automatic_organization_enabled: boolean;
  items: PipelineQueueItem[];
  title_dispositions?: Array<{
    disc_fingerprint: string;
    title_index: number;
    disposition: string;
    reason: string;
  }>;
  disc_matching_scopes?: Array<{
    disc_fingerprint: string;
    relevant_title_indexes: number[];
  }>;
  disc_recovery_scopes?: Array<{
    disc_fingerprint: string;
    required_title_indexes: number[];
  }>;
}

const requiresDiscWideTvRecovery = (item: PipelineQueueItem): boolean => {
  const reviewCode = item.review_code || '';
  if (episodeSequenceReviewCodes.has(reviewCode)) return true;
  if (!item.disc_fingerprint || !tvEpisodeProviderReviewCodes.has(reviewCode)) return false;
  return (item.identification_attempts ?? []).some((attempt) => (
    ['tv-local', 'tv-opensubtitles', 'tv-gemini', 'tv-disc-range'].includes(attempt.branch)
  ));
};

const episodeReviewCandidates = (item: PipelineQueueItem): Array<{ name: string; source: string; score: number | null; margin: number | null }> => {
  const attempts = item.identification_attempts ?? [];
  const rangeScope = [...attempts].reverse().find((attempt) => (
    attempt.branch === 'tv-disc-range'
    && attempt.summary.reason === 'evidence_anchored_candidate_fence_applied'
    && typeof attempt.summary.candidate_scope === 'string'
  ))?.summary.candidate_scope;
  const rangeMatch = typeof rangeScope === 'string'
    ? /^S(\d{1,2})E(\d{1,3})-E(\d{1,3})$/i.exec(rangeScope)
    : null;
  const range = rangeMatch ? {
    season: Number(rangeMatch[1]),
    firstEpisode: Number(rangeMatch[2]),
    lastEpisode: Number(rangeMatch[3]),
  } : null;
  const candidates = new Map<string, { name: string; source: string; score: number | null; margin: number | null }>();
  for (const attempt of attempts) {
    if (
      !['tv-local', 'tv-opensubtitles', 'tv-gemini'].includes(attempt.branch)
      || attempt.disposition !== 'review'
      || attempt.summary.phase === 'outside-season-review'
      || attempt.summary.reason === 'outside_explicit_season_boundary'
      || attempt.summary.reason === 'advisory_sequence_candidate'
    ) continue;
    const series = attempt.summary.candidate_series_name ?? item.series_name;
    const episodeId = attempt.summary.candidate_episode_id ?? attempt.summary.selected_episode_id;
    const title = attempt.summary.candidate_episode_title ?? attempt.summary.selected_episode_title;
    if (typeof series !== 'string' || typeof episodeId !== 'string' || typeof title !== 'string') continue;
    const episodeMatch = /^S(\d{1,2})E(\d{1,3})$/i.exec(episodeId);
    if (range && (
      !episodeMatch
      || Number(episodeMatch[1]) !== range.season
      || Number(episodeMatch[2]) < range.firstEpisode
      || Number(episodeMatch[2]) > range.lastEpisode
    )) continue;
    const name = `${series} - ${episodeId} - ${title}`;
    const confidence = attempt.summary.confidence ?? attempt.summary.best_score ?? attempt.summary.selected_score;
    const margin = attempt.summary.margin ?? attempt.summary.decision_margin;
    candidates.set(name, {
      name,
      source: attempt.branch === 'tv-opensubtitles' ? 'OpenSubtitles' : attempt.branch === 'tv-gemini' ? 'Gemini' : 'Local dialogue',
      score: typeof confidence === 'number' ? confidence : null,
      margin: typeof margin === 'number' ? margin : null,
    });
  }
  return Array.from(candidates.values());
};

interface IdentificationAuditEvent {
  analysis_run_id?: string | null;
  phase?: string | null;
  branch?: string | null;
  event_kind?: string | null;
  disposition: string;
  summary: MatchEvidenceSummary;
}

interface DiscIdentificationAudit {
  titles: Array<{
    media_id: string;
    identification_audit: IdentificationAuditEvent[];
  }>;
}

interface CollisionAudioTrackDetails {
  index: number;
  codec: string | null;
  language: string | null;
  title: string | null;
  channels: number | null;
  channel_layout: string | null;
  bitrate_bps: number | null;
  sample_rate_hz: number | null;
  default: boolean;
  commentary: boolean;
}

interface CollisionFileDetails {
  modified_at: string;
  size_bytes: number;
  container: string | null;
  video_codec: string | null;
  audio_codecs: string[];
  width: number | null;
  height: number | null;
  field_order: string | null;
  duration_seconds: number;
  overall_bitrate_bps: number | null;
  overall_bitrate_source: string | null;
  video_bitrate_bps: number | null;
  frame_rate_fps: number | null;
  video_profile: string | null;
  pixel_format: string | null;
  bit_depth: number | null;
  hdr_format: string | null;
  color_primaries: string | null;
  color_transfer: string | null;
  color_space: string | null;
  color_range: string | null;
  video_encoder: string | null;
  format_encoder: string | null;
  audio_tracks: CollisionAudioTrackDetails[];
}

interface LibraryCollisionComparison {
  media_id: string;
  new_pipeline_file: CollisionFileDetails;
  existing_jellyfin_file: CollisionFileDetails;
  size_difference_bytes: number;
}

interface SilentVideoOcrResult {
  media_id: string;
  category: string;
  summary: string;
  ocr_excerpt: string;
  ocr_text_characters: number;
  sampled_frame_count: number;
}

interface MatchingPerformanceRun {
  run_id: string;
  disc_fingerprint: string;
  series_name: string;
  title_count: number;
  anchor_count: number;
  season_scope: number[];
  proposed_count: number;
  applied_count: number;
  unresolved_count: number;
  anchor_elapsed_ms: number;
  total_elapsed_ms: number;
  outcome: 'completed' | 'failed';
  failure_stage: string | null;
  failure_code: string | null;
  provider_branches: string[];
  created_at: string;
}

interface LearnedSeriesCoverage {
  series_name: string;
  disc_count: number;
  episode_count: number;
  discs: Array<{
    disc_fingerprint: string;
    assigned_title_count: number;
    episode_count: number;
    other_title_count: number;
    seasons: number[];
    episode_ids: string[];
    assignments: Array<{ title_index: number; episode_id: string | null }>;
  }>;
}

interface TranscodeAuthorizationPlan {
  media_ids: string[];
  item_count: number;
  plan_sha256: string;
  default_profile_id: string;
  profile_display_name: string;
  profile_selection: 'explicit' | 'source-resolution';
  output_destination: string;
  organization_authorized: false;
}

interface OrganizationAuthorizationPlan {
  media_ids: string[];
  item_count: number;
  tv_count: number;
  movie_count: number;
  collision_media_ids: string[];
  collision_count: number;
  items: Array<{
    media_id: string;
    destination_relative: string;
    kind: 'tv' | 'movie';
    collision: boolean;
  }>;
  plan_sha256: string;
  operation: 'move-verified-encode';
  overwrite_authorized: false;
}

const responsePayload = async (response: Response): Promise<Record<string, unknown>> => {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { detail: response.ok ? 'The server returned an unreadable response.' : text.slice(0, 200) };
  }
};

const formatBytes = (value: number | null) => {
  if (!value) return 'Unknown';
  return `${(value / (1024 ** 3)).toFixed(2)} GiB`;
};

const formatActivityAge = (seconds: number | null | undefined) => {
  if (seconds === null || seconds === undefined) return 'an unknown interval';
  if (seconds < 60) return 'less than a minute';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} minute(s)`;
  return `${(seconds / 3600).toFixed(1)} hour(s)`;
};

const evidenceLabels: Record<string, string> = {
  policy: 'Matching policy',
  segment_threshold: 'Per-window threshold',
  engine_threshold: 'Final acceptance threshold',
  reference_variant_count: 'Subtitle variants compared',
  duration_seconds: 'Media duration (seconds)',
  successful_segment_count: 'Windows with candidates',
  empty_segment_count: 'Windows without candidates',
  candidate_segment_count: 'Windows sampled',
  selected_episode_id: 'Selected episode',
  selected_episode_title: 'Selected episode title',
  selected_score: 'Selected score',
  selected_vote_count: 'Selected window votes',
  selected_score_sum: 'Selected score total',
  runner_up_episode_id: 'Runner-up episode',
  runner_up_vote_count: 'Runner-up window votes',
  supplemental_attempted: 'Offset-window retry used',
  supplemental_segment_count: 'Additional windows sampled',
  supplemental_reason: 'Offset-window retry reason',
  subtitle_reference_pass: 'Subtitle matching pass',
  initial_reference_variant_count: 'Initial subtitle versions',
  alternate_reference_variant_count: 'Alternate subtitle versions',
  alternate_lookup_attempted: 'Alternate-release lookup attempted',
  initial_engine_reason: 'Initial-pass result',
  engine_reason: 'Final decision reason',
  sample_start_seconds: 'Sample position (seconds)',
  sample_duration_seconds: 'Sample length (seconds)',
  episode_candidate_count: 'Episodes scored in window',
  qualifying_candidate_count: 'Episodes over window threshold',
  best_episode_id: 'Window leader',
  rank: 'Candidate rank',
  qualified: 'Exceeded window threshold',
  subtitle_release_match: 'Subtitle edition class',
  subtitle_release_name: 'Subtitle release name',
  best_score: 'Best score',
  runner_up_score: 'Runner-up score',
  score: 'Score',
  margin: 'Decision margin',
  confidence: 'Provider confidence',
  candidate_episode_id: 'Candidate episode',
  candidate_scope: 'Candidate scope',
  candidate_count: 'Candidates compared',
  anchor_count: 'Strong sibling anchors',
  anchor_episode_ids: 'Sibling anchor episodes',
  disc_title_count: 'Known titles on disc',
  settled_sibling_count: 'Settled sibling titles',
  decision_margin: 'Decision margin',
  title_order_used: 'Title order used as evidence',
  qualifying_window_count: 'Qualifying transcript windows',
  subtitle_release_profile: 'Requested subtitle edition',
  subtitle_reference_variant_count: 'Subtitle versions compared',
  subtitle_reference_episode_count: 'Episodes with subtitles',
  subtitle_exact_reference_count: 'Exact-edition subtitles',
  subtitle_compatible_reference_count: 'Compatible extended subtitles',
  subtitle_generic_reference_count: 'Regular-edition fallbacks',
  subtitle_unresolved_reference_count: 'Unclassified subtitle versions',
  runtime_consistent: 'Runtime consistent',
  reason: 'Reason',
  duration_ratio: 'Duration ratio',
  size_ratio: 'Size ratio',
  component_episode_ids: 'Covered episodes',
};

const formatEvidenceValue = (key: string, value: string | number | boolean | null | string[]) => {
  if (value === null) return 'Not available';
  if (Array.isArray(value)) return value.join(', ');
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'number') {
    if (['best_score', 'runner_up_score', 'score', 'margin', 'confidence'].includes(key)) {
      return `${Math.round(value * 100)}%`;
    }
    if (['duration_ratio', 'size_ratio'].includes(key)) return value.toFixed(2);
    return String(value);
  }
  return key === 'reason' ? value.replaceAll('_', ' ') : value;
};

const matchEvidenceEntries = (summary: MatchEvidenceSummary) => (
  Object.entries(summary).filter(([key]) => key in evidenceLabels)
);

const allSeasonAnalysisRunning = (item: PipelineQueueItem) => item.review_code === 'all_season_analysis_running';

const episodeCandidateDetail = (candidate: { score: number | null; margin: number | null }) => (
  `${candidate.score === null ? '' : ` · ${Math.round(candidate.score * 100)}% similarity`}`
  + `${candidate.margin === null ? '' : ` · ${Math.round(candidate.margin * 100)}% lead over runner-up`}`
);

const pipelineItemElementId = (mediaId: string) => `pipeline-item-${mediaId}`;

const formatCollisionDate = (value: string) => new Date(value).toLocaleString();

const formatCollisionResolution = (file: CollisionFileDetails) => {
  if (!file.width || !file.height) return 'Unknown';
  const scan = file.field_order === 'progressive' ? 'p' : file.field_order && file.field_order !== 'unknown' ? 'i' : '';
  return `${file.width} × ${file.height}${scan}`;
};

const formatCollisionDuration = (value: number) => {
  if (!Number.isFinite(value) || value <= 0) return 'Unknown';
  const seconds = Math.round(value);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
};

const formatCollisionBitrate = (value: number | null, source?: string | null) => {
  if (value === null) return 'Not reported';
  const bitrate = value >= 1_000_000 ? `${(value / 1_000_000).toFixed(2)} Mb/s` : `${Math.round(value / 1000)} kb/s`;
  return source === 'size-duration' ? `${bitrate} (from size and duration)` : bitrate;
};

const formatCollisionPixel = (file: CollisionFileDetails) => (
  [file.pixel_format, file.bit_depth ? `${file.bit_depth}-bit` : null].filter(Boolean).join(' · ') || 'Not reported'
);

const formatCollisionColor = (file: CollisionFileDetails) => (
  [file.hdr_format, file.color_primaries, file.color_transfer, file.color_space, file.color_range].filter(Boolean).join(' · ') || 'Not reported'
);

const formatCollisionEncoder = (file: CollisionFileDetails) => (
  [file.video_encoder, file.format_encoder && file.format_encoder !== file.video_encoder ? file.format_encoder : null].filter(Boolean).join(' · ') || 'Not reported'
);

const formatCollisionAudioTrack = (file: CollisionFileDetails, index: number) => {
  const track = file.audio_tracks[index];
  if (!track) return 'None';
  return [
    track.codec || 'unknown codec',
    track.language || 'language unknown',
    track.channels ? `${track.channels} ch` : null,
    track.channel_layout,
    formatCollisionBitrate(track.bitrate_bps),
    track.sample_rate_hz ? `${(track.sample_rate_hz / 1000).toFixed(1)} kHz` : null,
    track.default ? 'default' : null,
    track.commentary ? 'commentary' : null,
    track.title,
  ].filter(Boolean).join(' · ');
};

const IdentificationAuditPanel = ({
  item,
  audit,
  loading,
  error,
  onLoad,
}: {
  item: PipelineQueueItem;
  audit: DiscIdentificationAudit | null;
  loading: boolean;
  error: string | null;
  onLoad: () => void;
}) => {
  if (!item.disc_fingerprint) return null;
  const events = audit?.titles.find((title) => title.media_id === item.media_id)?.identification_audit ?? [];
  return (
    <div className="mt-2 max-w-3xl text-xs">
      <button type="button" className="btn btn-secondary text-xs" disabled={loading} onClick={onLoad}>
        {loading ? 'Loading complete matching log…' : audit ? 'Refresh complete matching log' : 'Load complete matching log'}
      </button>
      {error && <div className="mt-2 rounded border border-red-400/30 bg-red-500/10 p-2 text-red-100">{error}</div>}
      {audit && events.length === 0 && (
        <div className="mt-2 rounded border border-amber-400/30 bg-amber-500/10 p-2 text-amber-100">
          No detailed matcher events were retained for this older run. Retry identification after restarting RipWeaver to populate the new audit trail.
        </div>
      )}
      {events.length > 0 && (
        <details className="mt-2 rounded border border-[var(--border-color)] p-2">
          <summary className="cursor-pointer font-semibold text-white">Complete matching log · {events.length} event{events.length === 1 ? '' : 's'}</summary>
          <div className="mt-2 max-h-[32rem] divide-y divide-[var(--border-color)] overflow-auto pr-2">
            {events.map((event, index) => (
              <div className="py-2" key={`${event.analysis_run_id ?? 'run'}-${index}`}>
                <div className="flex flex-wrap gap-x-2 font-semibold text-white">
                  <span>{event.phase ?? event.branch ?? event.event_kind}</span>
                  <span className="text-[var(--text-muted)]">{event.disposition.replaceAll('_', ' ')}</span>
                </div>
                <dl className="mt-1 grid grid-cols-[minmax(9rem,auto)_1fr] gap-x-3 gap-y-1 text-[var(--text-muted)]">
                  {Object.entries(event.summary).map(([key, value]) => (
                    <Fragment key={key}>
                      <dt>{evidenceLabels[key] ?? key.replaceAll('_', ' ')}</dt>
                      <dd className="break-words text-white">{formatEvidenceValue(key, value)}</dd>
                    </Fragment>
                  ))}
                </dl>
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
};

const pipelineStages = ['rip', 'identify', 'transcode', 'organize'] as const;
const stageIcon: Record<string, string> = {
  rip: '💿', identify: '🔎', transcode: '🎞️', organize: '📁', complete: '✅',
};

const hasSavedIdentification = (item: PipelineQueueItem): boolean => Boolean(item.display_name);

const isRecoveryPipelineItem = (item: PipelineQueueItem): boolean => item.media_id.includes('-recovery-');

const preferredPipelineItem = (
  current: PipelineQueueItem | undefined,
  candidate: PipelineQueueItem,
): PipelineQueueItem => {
  if (!current) return candidate;
  if (candidate.state === 'discarded' && current.state !== 'discarded') return current;
  if (current.state === 'discarded' && candidate.state !== 'discarded') return candidate;

  const currentIsRecovery = isRecoveryPipelineItem(current);
  const candidateIsRecovery = isRecoveryPipelineItem(candidate);
  if (currentIsRecovery !== candidateIsRecovery) {
    // A recovery item is the current logical attempt for this disc/title. A
    // later retry timestamp on the superseded original must not hide it.
    return candidateIsRecovery ? candidate : current;
  }

  return Date.parse(candidate.updated_at) >= Date.parse(current.updated_at)
    ? candidate
    : current;
};

const pipelineStatusLabel = (item: PipelineQueueItem, processingPaused = false) => {
  const action = {
    rip: { active: 'Ripping now', waiting: 'Waiting to rip' },
    identify: { active: 'Matching now', waiting: 'Waiting to match' },
    transcode: { active: 'Transcoding now', waiting: 'Waiting to transcode' },
    organize: { active: 'Transferring to Jellyfin now', waiting: 'Waiting to transfer to Jellyfin' },
  }[item.stage];
  if (item.state === 'running') return action?.active || `${item.stage} running`;
  if (item.state === 'queued' && item.stage === 'identify' && hasSavedIdentification(item)) {
    return processingPaused
      ? 'Match found · resume processing to continue'
      : 'Match found · waiting to continue';
  }
  if (item.state === 'queued' && processingPaused) {
    return item.stage === 'identify'
      ? 'Ready to match · resume processing'
      : `Ready to ${item.stage} · resume processing`;
  }
  if (item.state === 'queued') return action?.waiting || `${item.stage} queued`;
  if (item.state === 'review_required') return item.review_code === 'all_season_analysis_running' ? 'Running all-season matching' : item.review_code === 'play_all_aggregate_detected' ? 'Likely play-all aggregate' : 'Review required';
  if (item.state === 'failed') return 'Stopped after an error';
  if (item.state === 'completed') return 'Completed';
  return `${item.stage} · ${item.state.replaceAll('_', ' ')}`;
};

const pipelineErrorHelp: Record<string, string> = {
  HandBrakeError: 'This older failure did not retain a specific diagnostic. Retry once to generate an actionable category.',
  HandBrakePreflightFailed: 'HandBrake did not start because a preflight requirement failed. Review the current profile and tool configuration.',
  HandBrakePartialExists: 'A previous partial output was preserved. Retry verifies it first and uses a new collision-safe attempt when needed.',
  HandBrakeEncoderUnavailable: 'The selected video encoder is unavailable. Choose a supported HandBrake profile or correct the GPU/driver setup.',
  HandBrakeAudioLanguageMissing: 'The requested audio language was not present. Use a profile that allows the source default audio.',
  HandBrakeNoUsableAudio: 'No usable audio track was detected. This may be a silent menu or still-image bonus item and needs content review.',
  HandBrakeNoUsableVideo: 'No usable video stream was detected. This title cannot be transcoded as an ordinary video.',
  HandBrakeAudioInspectionFailed: 'FFprobe could not validate the source audio metadata. Review the staged source before retrying.',
  HandBrakeVideoInspectionFailed: 'FFprobe could not validate the source video metadata. Review the staged source before retrying.',
  HandBrakeTimedOut: 'HandBrake exceeded the configured time limit. The partial was preserved for verification on retry.',
  HandBrakeDestinationExists: 'The encoded staging destination already exists and was preserved. Resolve the duplicate before retrying.',
  HandBrakeNoOutput: 'HandBrake exited without producing an output file.',
  HandBrakeOutputVerificationFailed: 'HandBrake produced output, but post-encode verification rejected it. The partial remains preserved.',
  HandBrakeProcessFailed: 'HandBrake started but returned a failure. Review the retained process diagnostics before retrying.',
};

const pipelineStageClass = (item: PipelineQueueItem, stage: typeof pipelineStages[number]) => {
  const stageIndex = pipelineStages.indexOf(stage);
  const currentIndex = pipelineStages.indexOf(item.stage as typeof pipelineStages[number]);
  if (stageIndex < currentIndex || item.state === 'completed') return 'bg-green-500/20 text-green-200 border-green-400/40';
  if (stageIndex > currentIndex) return 'bg-white/5 text-[var(--text-muted)] border-transparent';
  if (item.state === 'running') return 'bg-blue-500/25 text-blue-100 border-blue-300/60 animate-pulse';
  if (item.state === 'queued') return 'bg-amber-500/20 text-amber-100 border-amber-300/50';
  if (item.state === 'failed') return 'bg-red-500/20 text-red-100 border-red-400/50';
  if (item.state === 'review_required') return item.review_code?.endsWith('_running')
    ? 'bg-blue-500/25 text-blue-100 border-blue-300/60 animate-pulse'
    : 'bg-orange-500/20 text-orange-100 border-orange-300/50';
  return 'bg-indigo-500/20 text-indigo-200 border-indigo-400/40';
};

interface RipPipelineViewProps {
  onOpenSettings?: () => void;
  onOpenDashboard?: () => void;
  queueOnly?: boolean;
  attentionOnly?: boolean;
}

const RipPipelineView = ({ onOpenSettings, onOpenDashboard, queueOnly = false, attentionOnly = false }: RipPipelineViewProps) => {
  const [reportText, setReportText] = useState('');
  const [seriesName, setSeriesName] = useState('');
  const [season, setSeason] = useState('1');
  const [outputRoot, setOutputRoot] = useState('');
  const [preview, setPreview] = useState<RipPreview | null>(null);
  const [previewRequest, setPreviewRequest] = useState<PreviewRequest | null>(null);
  const [creationKey, setCreationKey] = useState('');
  const [savedJob, setSavedJob] = useState<OrchestrationJob | null>(null);
  const [error, setError] = useState('');
  const [reviewNotice, setReviewNotice] = useState('');
  const [selectingTitles, setSelectingTitles] = useState(false);
  const [selectedTitleIndexes, setSelectedTitleIndexes] = useState<number[]>([]);
  const [existingRecoveryPlan, setExistingRecoveryPlan] = useState<ExistingRipRecoveryPlan | null>(null);
  const [recoverableExistingTitleCount, setRecoverableExistingTitleCount] = useState(0);
  const [recoverableExistingTitleIndexes, setRecoverableExistingTitleIndexes] = useState<number[]>([]);
  const [existingRipsRestarted, setExistingRipsRestarted] = useState(false);
  const [selectedRecoveryCandidates, setSelectedRecoveryCandidates] = useState<Record<number, string>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [controlling, setControlling] = useState(false);
  const [geminiProgress, setGeminiProgress] = useState<Record<string, string>>({});
  const [sceneDescriptionDrafts, setSceneDescriptionDrafts] = useState<Record<string, string>>({});
  const [submittingSceneReviewId, setSubmittingSceneReviewId] = useState<string | null>(null);
  const [silentVideoOcr, setSilentVideoOcr] = useState<Record<string, SilentVideoOcrResult>>({});
  const [silentVideoOcrErrors, setSilentVideoOcrErrors] = useState<Record<string, string>>({});
  const [silentVideoOcrRunningId, setSilentVideoOcrRunningId] = useState<string | null>(null);
  const [collisionComparisons, setCollisionComparisons] = useState<Record<string, LibraryCollisionComparison>>({});
  const [collisionComparisonErrors, setCollisionComparisonErrors] = useState<Record<string, string>>({});
  const [inspectingCollisionId, setInspectingCollisionId] = useState<string | null>(null);
  const [showLikelyRemovableOnly, setShowLikelyRemovableOnly] = useState(false);
  const [ejectingDrives, setEjectingDrives] = useState<number[]>([]);
  const [queuedEjectDrives, setQueuedEjectDrives] = useState<number[]>([]);
  const [ejectFailureByDrive, setEjectFailureByDrive] = useState<Record<number, string>>({});
  const ejectQueueRef = useRef<Promise<void>>(Promise.resolve());
  const ejectDriveRef = useRef<(drive: DriveSlot, alreadyConfirmed?: boolean, quietFailure?: boolean) => Promise<boolean>>(
    () => Promise.resolve(false),
  );
  const [completeDiscAutoEjectDeadlines, setCompleteDiscAutoEjectDeadlines] = useState<Record<string, number>>({});
  const [completeDiscAutoEjectHolds, setCompleteDiscAutoEjectHolds] = useState<Record<string, 'kept' | 'failed'>>({});
  const [completeDiscAutoEjectClock, setCompleteDiscAutoEjectClock] = useState(() => Date.now());
  const completeDiscAutoEjectAttemptedRef = useRef<Set<string>>(new Set());
  const [renameDrafts, setRenameDrafts] = useState<Record<string, string>>({});
  const [seriesDrafts, setSeriesDrafts] = useState<Record<string, string>>({});
  const [bonusReviewModes, setBonusReviewModes] = useState<Record<string, boolean>>({});
  const [confirmPhysicalRip, setConfirmPhysicalRip] = useState(false);
  const [preserveFailedPartials, setPreserveFailedPartials] = useState(false);
  const [failedCleanupPlan, setFailedCleanupPlan] = useState<FailedRipCleanupPlan | null>(null);
  const [pipelineQueue, setPipelineQueue] = useState<PipelineQueue | null>(null);
  const pipelineQueueRef = useRef<PipelineQueue | null>(null);
  const [matchingPerformance, setMatchingPerformance] = useState<MatchingPerformanceRun[]>([]);
  const [learnedCoverage, setLearnedCoverage] = useState<LearnedSeriesCoverage | null>(null);
  const [transcodePlan, setTranscodePlan] = useState<TranscodeAuthorizationPlan | null>(null);
  const [organizationPlan, setOrganizationPlan] = useState<OrganizationAuthorizationPlan | null>(null);
  const [transcodeProfile, setTranscodeProfile] = useState('');
  const [jobDashboard, setJobDashboard] = useState<JobDashboard | null>(null);
  const jobDashboardRef = useRef<JobDashboard | null>(null);
  const [defaultProfile, setDefaultProfile] = useState('Default');
  const [rememberLastProfile, setRememberLastProfile] = useState(true);
  const [discSetups, setDiscSetups] = useState<Record<string, DiscSetup>>({});
  const [driveDashboard, setDriveDashboard] = useState<DriveDashboard | null>(null);
  const driveDashboardRef = useRef<DriveDashboard | null>(null);
  const savedJobRef = useRef<OrchestrationJob | null>(null);
  const [refreshingDrives, setRefreshingDrives] = useState(false);
  const [showDriveMappingWizard, setShowDriveMappingWizard] = useState(false);
  const [mappingDraft, setMappingDraft] = useState<Record<string, 'trusted' | 'ignored'>>({});
  const [savingDriveMappings, setSavingDriveMappings] = useState(false);
  const [continueAfterMapping, setContinueAfterMapping] = useState(true);
  const reviewedMappingSnapshotRef = useRef<string | null>(null);
  const dashboardRefreshRunningRef = useRef(false);
  const [preparingDrive, setPreparingDrive] = useState<number | null>(null);
  const [queuedPrepareDrives, setQueuedPrepareDrives] = useState<number[]>([]);
  const prepareQueue = useRef<Promise<void>>(Promise.resolve());
  const [handbrakeProfiles, setHandbrakeProfiles] = useState<StoredProfile[]>([]);
  const [automaticGeminiFallback, setAutomaticGeminiFallback] = useState(false);
  const [automaticEjectAfterCompletion, setAutomaticEjectAfterCompletion] = useState(false);
  const [discGeminiFallback, setDiscGeminiFallback] = useState(false);
  const [jellyfinRoots, setJellyfinRoots] = useState({ tv: '', movie: '' });
  const [retainedSourceTtlDays, setRetainedSourceTtlDays] = useState(30);
  const [unmatchedSeriesName, setUnmatchedSeriesName] = useState('');
  const [unmatchedEpisodeStart, setUnmatchedEpisodeStart] = useState('');
  const [unmatchedEpisodeEnd, setUnmatchedEpisodeEnd] = useState('');
  const [submittingSeriesRecovery, setSubmittingSeriesRecovery] = useState<string | null>(null);
  const [openingReviewId, setOpeningReviewId] = useState<string | null>(null);
  const [reviewPlaybackOpened, setReviewPlaybackOpened] = useState<Set<string>>(() => new Set());
  const [rerippingJobId, setRerippingJobId] = useState<string | null>(null);
  const [executingJobId, setExecutingJobId] = useState<string | null>(null);
  const [reviewingItemId, setReviewingItemId] = useState<string | null>(null);
  const [catalogueSupportStatus, setCatalogueSupportStatus] = useState<CatalogueSupportStatus | null>(null);
  const [supportAmountCents, setSupportAmountCents] = useState(1000);
  const [supportRateCents, setSupportRateCents] = useState(10);
  const [supportTermsAccepted, setSupportTermsAccepted] = useState(false);
  const [startingSupportCheckout, setStartingSupportCheckout] = useState(false);
  const [identificationAudits, setIdentificationAudits] = useState<Record<string, DiscIdentificationAudit>>({});
  const [identificationAuditErrors, setIdentificationAuditErrors] = useState<Record<string, string>>({});
  const [loadingIdentificationAudit, setLoadingIdentificationAudit] = useState<string | null>(null);

  const loadIdentificationAudit = async (discFingerprint: string) => {
    setLoadingIdentificationAudit(discFingerprint);
    setIdentificationAuditErrors((current) => ({ ...current, [discFingerprint]: '' }));
    try {
      const response = await fetch(`/rip/pipeline/discs/${discFingerprint}/identification-audit`);
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'The complete matching log could not be loaded.');
      setIdentificationAudits((current) => ({ ...current, [discFingerprint]: payload as unknown as DiscIdentificationAudit }));
    } catch (requestError) {
      setIdentificationAuditErrors((current) => ({
        ...current,
        [discFingerprint]: requestError instanceof Error ? requestError.message : 'The complete matching log could not be loaded.',
      }));
    } finally {
      setLoadingIdentificationAudit((current) => current === discFingerprint ? null : current);
    }
  };

  useEffect(() => {
    pipelineQueueRef.current = pipelineQueue;
  }, [pipelineQueue]);

  useEffect(() => {
    jobDashboardRef.current = jobDashboard;
  }, [jobDashboard]);

  useEffect(() => {
    driveDashboardRef.current = driveDashboard;
  }, [driveDashboard]);

  useEffect(() => {
    savedJobRef.current = savedJob;
  }, [savedJob]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const schedule = (delay: number) => {
      if (!cancelled && document.visibilityState !== 'hidden') timer = window.setTimeout(refresh, delay);
    };
    const refresh = async () => {
      if (cancelled || document.visibilityState === 'hidden') return;
      if (dashboardRefreshRunningRef.current) return;
      dashboardRefreshRunningRef.current = true;
      try {
        let driveSnapshot = queueOnly || attentionOnly ? null : driveDashboardRef.current;
        if (!queueOnly && !attentionOnly) {
          const response = await sharedDashboardJson<DriveDashboard>('/rip/drives');
          if (response.ok && response.payload) {
            driveSnapshot = response.payload;
            if (!cancelled) setDriveDashboard(driveSnapshot);
          }
        }
        const fingerprints = dashboardDiscFingerprints(driveSnapshot, savedJobRef.current, jobDashboardRef.current);
        const pipelineScope = attentionOnly ? 'attention' : queueOnly ? 'active' : 'dashboard';
        const jobsScope = queueOnly ? 'active' : 'dashboard';
        const pipelineRequest = sharedDashboardJson<PipelineQueue>(scopedDashboardUrl('/rip/pipeline/items', pipelineScope, pipelineScope === 'dashboard' ? fingerprints : []));
        const jobsRequest = attentionOnly
          ? Promise.resolve<DashboardJsonResult<JobDashboard> | null>(null)
          : sharedDashboardJson<JobDashboard>(scopedDashboardUrl('/rip/jobs', jobsScope, queueOnly ? [] : fingerprints));
        const performanceRequest = !attentionOnly && Date.now() - lastMatchingPerformanceRefreshAt >= 60_000
          ? sharedDashboardJson<{ runs: MatchingPerformanceRun[] }>('/rip/pipeline/matching-performance')
          : Promise.resolve<DashboardJsonResult<{ runs: MatchingPerformanceRun[] }> | null>(null);
        const [pipelineResponse, jobsResponse, performanceResponse] = await Promise.all([
          pipelineRequest,
          jobsRequest,
          performanceRequest,
        ]);
        let queueSnapshot = pipelineQueueRef.current;
        let jobSnapshot = attentionOnly ? null : jobDashboardRef.current;
        if (pipelineResponse.ok && pipelineResponse.payload) {
          queueSnapshot = pipelineResponse.payload;
          if (!cancelled) setPipelineQueue(queueSnapshot);
        }
        if (jobsResponse?.ok && jobsResponse.payload) {
          jobSnapshot = jobsResponse.payload;
          if (!cancelled) setJobDashboard(jobSnapshot);
        }
        if (performanceResponse?.ok && performanceResponse.payload) {
          lastMatchingPerformanceRefreshAt = Date.now();
          if (!cancelled) setMatchingPerformance(performanceResponse.payload.runs);
        }
        schedule(dashboardHasActiveWork(driveSnapshot, jobSnapshot, queueSnapshot) ? 3000 : 12_000);
      } catch {
        schedule(12_000);
      } finally {
        dashboardRefreshRunningRef.current = false;
      }
    };
    const handleVisibility = () => {
      if (timer !== undefined) window.clearTimeout(timer);
      if (document.visibilityState === 'visible') void refresh();
    };
    document.addEventListener('visibilitychange', handleVisibility);
    void refresh();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, [attentionOnly, queueOnly]);

  useEffect(() => {
    const selectedJobId = savedJobRef.current?.job_id;
    if (!selectedJobId) return;
    const refreshed = jobDashboard?.jobs.find((job) => job.job_id === selectedJobId);
    if (refreshed) setSavedJob(refreshed);
  }, [jobDashboard]);

  useEffect(() => {
    if (!preview?.drives.some((drive) => drive.metadata_status === 'support-required')) return;
    const loadCatalogueStatus = async () => {
      try {
        const response = await fetch('/catalogue/status');
        if (!response.ok) return;
        const status = await response.json() as CatalogueSupportStatus;
        setCatalogueSupportStatus(status);
        if (status.policy) {
          setSupportAmountCents((current) => Math.max(current, status.policy?.minimum_amount_cents ?? 1000));
          setSupportRateCents(status.policy.default_rate_cents);
        }
      } catch {
        // The manual continuation remains available when status is offline.
      }
    };
    void loadCatalogueStatus();
  }, [preview]);

  useEffect(() => {
    const loadProfiles = async () => {
      const response = await fetch('/rip/handbrake/profiles');
      if (!response.ok) return;
      const payload = await response.json() as { profiles: StoredProfile[] };
      setHandbrakeProfiles(payload.profiles);
    };
    void loadProfiles();
  }, []);

  useEffect(() => {
    if (!driveDashboard?.mapping_plan_sha256) return;
    const snapshotKey = `${driveDashboard.mapping_plan_sha256}:${Boolean(jobDashboard?.automatic_processing_enabled)}`;
    if (reviewedMappingSnapshotRef.current === snapshotKey) return;
    reviewedMappingSnapshotRef.current = snapshotKey;
    const mappedDevices = driveDashboard.drives.filter((drive) => drive.mapping_id);
    if (mappedDevices.length === 0) return;
    setMappingDraft(Object.fromEntries(mappedDevices.map((drive) => [
      drive.mapping_id as string,
      drive.mapping_status === 'ignored' ? 'ignored' : 'trusted',
    ])));
    const needsReview = mappedDevices.some((drive) => (
      drive.mapping_status === 'unmapped' || drive.mapping_warning === 'possible_identity_change'
    ));
    if (needsReview) setShowDriveMappingWizard(true);
    setContinueAfterMapping(Boolean(
      jobDashboard?.automatic_processing_enabled
      && mappedDevices.some((drive) => drive.has_disc && drive.makemkv_confirmed !== false),
    ));
  }, [driveDashboard?.drives, driveDashboard?.mapping_plan_sha256, jobDashboard?.automatic_processing_enabled]);

  useEffect(() => {
    const loadConfig = async () => {
      const response = await fetch('/system/config');
      if (!response.ok) return;
      const config = await response.json() as PublicConfig;
      if (config.default_handbrake_profile?.trim()) {
        setDefaultProfile(config.default_handbrake_profile.trim());
        setTranscodeProfile('auto-resolution');
      }
      setRememberLastProfile(config.remember_last_handbrake_profile !== false);
      setAutomaticEjectAfterCompletion(Boolean(config.automatic_eject_after_rip));
      setAutomaticGeminiFallback(Boolean(config.automatic_gemini_ambiguity_fallback));
      setDiscGeminiFallback(Boolean(config.automatic_gemini_ambiguity_fallback));
      setJellyfinRoots({ tv: config.jellyfin_tv_root || '', movie: config.jellyfin_movie_root || '' });
      setRetainedSourceTtlDays(Number(config.retained_source_ttl_days || 30));
    };
    void loadConfig();
  }, []);

  const fullLibraryPath = (item: PipelineQueueItem) => {
    if (!item.location_relative || !item.location_root_key) return null;
    const root = item.location_root_key === 'jellyfin_tv_root' ? jellyfinRoots.tv : jellyfinRoots.movie;
    if (!root) return null;
    return `${root.replace(/[\\/]+$/, '')}\\${item.location_relative.replaceAll('/', '\\')}`;
  };
  const getDiscSetup = (discId: string): DiscSetup => discSetups[discId] ?? {
    contentType: '',
    handbrakeProfile: '',
    addMissingOnly: false,
    automaticProfile: defaultProfile,
  };

  const driveSetupKey = (drive: DriveSlot) => `drive-${drive.mapping_id || drive.drive_index}-${drive.disc_label || 'loaded'}`;

  const updateDiscSetup = (discId: string, update: Partial<DiscSetup>) => {
    setDiscSetups((current) => ({
      ...current,
      [discId]: {
        ...(current[discId] ?? {
          contentType: '',
          handbrakeProfile: '',
          addMissingOnly: false,
          automaticProfile: defaultProfile,
        }),
        ...update,
      },
    }));
  };

  const rememberDefaultProfile = async (profileId: string) => {
    if (!profileId || !rememberLastProfile) return;
    setDiscSetups((current) => {
      const frozen = { ...current };
      driveDashboard?.drives.filter((drive) => drive.has_disc).forEach((drive) => {
        const key = driveSetupKey(drive);
        if (!frozen[key]) {
          frozen[key] = {
            contentType: '',
            handbrakeProfile: '',
            addMissingOnly: false,
            automaticProfile: defaultProfile,
          };
        }
      });
      return frozen;
    });
    try {
      const response = await fetch('/rip/handbrake/profiles/default', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile_id: profileId }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Default profile was not saved.');
      setDefaultProfile(profileId);
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'Default profile was not saved.';
      setError(message);
      window.alert(message);
    }
  };

  const refreshDrives = async (timeoutSeconds = 120) => {
    setRefreshingDrives(true);
    setError('');
    try {
      const response = await fetch('/rip/drives/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm_read: true, timeout_seconds: timeoutSeconds }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Drive refresh failed safely.');
      setDriveDashboard(payload as unknown as DriveDashboard);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Drive refresh failed safely.');
      const statusResponse = await fetch('/rip/drives');
      if (statusResponse.ok) setDriveDashboard(await statusResponse.json());
    } finally {
      setRefreshingDrives(false);
    }
  };

  const saveDriveMappingPlan = async () => {
    if (!driveDashboard?.mapping_plan_sha256) return;
    const devices = driveDashboard.drives.filter((drive): drive is DriveSlot & { mapping_id: string } => Boolean(drive.mapping_id));
    if (devices.length === 0 || devices.some((drive) => !mappingDraft[drive.mapping_id])) {
      setError('Choose Use or Ignore for every detected optical device.');
      return;
    }
    const continueLoaded = Boolean(
      continueAfterMapping
      && jobDashboard?.automatic_processing_enabled
      && devices.some((drive) => (
        drive.has_disc
        && drive.makemkv_confirmed !== false
        && mappingDraft[drive.mapping_id] === 'trusted'
      )),
    );
    setSavingDriveMappings(true);
    setError('');
    try {
      const response = await fetch('/rip/drives/mappings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_mapping_plan_sha256: driveDashboard.mapping_plan_sha256,
          mappings: devices.map((drive) => ({
            mapping_id: drive.mapping_id,
            status: mappingDraft[drive.mapping_id],
          })),
          retire_absent_trusted: true,
          continue_automatic_processing: continueLoaded,
          confirm_mapping: true,
          confirm_automatic_processing: continueLoaded,
        }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'The drive mapping was not saved.');
      const updated = payload as unknown as DriveDashboard;
      setDriveDashboard(updated);
      setShowDriveMappingWizard(false);
      const trusted = updated.mapping_summary?.trusted ?? devices.filter((drive) => mappingDraft[drive.mapping_id] === 'trusted').length;
      const ignored = updated.mapping_summary?.ignored ?? devices.length - trusted;
      const retired = updated.retired_mapping_count ?? 0;
      setReviewNotice(
        `Drive setup saved: ${trusted} trusted, ${ignored} ignored${retired ? `, ${retired} absent old ${retired === 1 ? 'identity' : 'identities'} retired` : ''}.${continueLoaded ? ' Loaded discs may now continue automatically.' : ''}`,
      );
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'The drive mapping was not saved.');
    } finally {
      setSavingDriveMappings(false);
    }
  };

  const ejectDrive = async (drive: DriveSlot, alreadyConfirmed = false, quietFailure = false): Promise<boolean> => {
    if (!alreadyConfirmed && !window.confirm(`${drive.has_disc ? 'Eject the disc from' : 'Open the tray for'} optical drive ${drive.drive_index + 1}${drive.disc_label ? ` — ${drive.disc_label}` : ''}? RipWeaver will refuse if this drive has active or queued rip work.`)) return false;
    if (queuedEjectDrives.includes(drive.drive_index) || ejectingDrives.includes(drive.drive_index)) return false;
    setQueuedEjectDrives((current) => [...current, drive.drive_index]);
    const operation = ejectQueueRef.current.catch(() => undefined).then(async () => {
      setQueuedEjectDrives((current) => current.filter((index) => index !== drive.drive_index));
      setEjectingDrives((current) => [...current, drive.drive_index]);
      setError('');
      setEjectFailureByDrive((current) => Object.fromEntries(
        Object.entries(current).filter(([index]) => Number(index) !== drive.drive_index),
      ));
      try {
        const response = await fetch(`/rip/drives/${drive.drive_index}/eject`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ confirm_eject: true, timeout_seconds: 30 }),
        });
        const payload = await responsePayload(response);
        if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'The disc could not be ejected safely.');
        setReviewNotice(`Ejected optical drive ${drive.drive_index + 1}.`);
        window.setTimeout(() => { void refreshDrives(); }, 1000);
        return true;
      } catch (requestError) {
        const message = requestError instanceof Error ? requestError.message : 'The disc could not be ejected safely.';
        setError(`Optical drive ${drive.drive_index + 1} was not ejected: ${message}`);
        setEjectFailureByDrive((current) => ({ ...current, [drive.drive_index]: message }));
        if (!quietFailure) window.alert(`Optical drive ${drive.drive_index + 1} was not ejected. ${message}`);
        return false;
      } finally {
        setEjectingDrives((current) => current.filter((index) => index !== drive.drive_index));
      }
    });
    ejectQueueRef.current = operation.then(() => undefined, () => undefined);
    return operation;
  };
  ejectDriveRef.current = ejectDrive;

  const forgetDiscIdentity = async (drive: DriveSlot, deleteStagedMedia = false) => {
    if (!drive.current_disc_fingerprint) return;
    const label = drive.disc_label || `optical drive ${drive.drive_index + 1}`;
    setControlling(true);
    setError('');
    try {
      let mediaPlan: { plan_sha256: string; file_count: number; total_size_bytes: number } | null = null;
      if (deleteStagedMedia) {
        const previewResponse = await fetch(`/rip/drives/${drive.drive_index}/forget-disc-identity/media-preview`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ expected_disc_fingerprint: drive.current_disc_fingerprint }),
        });
        const previewPayload = await responsePayload(previewResponse);
        if (!previewResponse.ok) throw new Error(typeof previewPayload.detail === 'string' ? previewPayload.detail : 'The staged MKV deletion preview could not be created safely.');
        mediaPlan = previewPayload as unknown as {
          plan_sha256: string;
          file_count: number;
          total_size_bytes: number;
        };
        if (!mediaPlan || mediaPlan.file_count < 1) {
          window.alert(`No exact fingerprint-bound staged MKVs were found for "${label}". Use the metadata-only forget action instead.`);
          return;
        }
        if (!window.confirm(`Permanently delete exactly ${mediaPlan.file_count} fingerprint-bound staged MKV file(s) (${formatBytes(mediaPlan.total_size_bytes)}) and forget the saved identity and matching history for "${label}"? Encoded files and Jellyfin media will not be changed. This cannot be undone.`)) return;
      } else if (!window.confirm(`Forget RipWeaver's saved identity and matching history for "${label}"? This removes only database records for this exact disc fingerprint. Staged MKVs, encoded files, Jellyfin media, and logs will not be deleted or changed.`)) {
        return;
      }
      const response = await fetch(`/rip/drives/${drive.drive_index}/forget-disc-identity`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_disc_fingerprint: drive.current_disc_fingerprint,
          confirm_forget: true,
          delete_staged_media: deleteStagedMedia,
          expected_media_plan_sha256: mediaPlan?.plan_sha256,
          authorized_media_file_count: mediaPlan?.file_count,
          confirm_delete_staged_media: deleteStagedMedia,
        }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'The saved disc identity could not be forgotten safely.');
      setSavedJob(null);
      setPreview(null);
      setReviewNotice(deleteStagedMedia
        ? `Forgot the saved identity for "${label}" and deleted ${mediaPlan?.file_count ?? 0} exact staged MKV file(s). The disc is ready for fresh preparation.`
        : `Forgot the saved identity for "${label}". No media files were changed. The disc is ready for fresh preparation.`);
      const [drivesResponse, jobsResponse, queueResponse] = await Promise.all([
        fetch('/rip/drives'), fetch('/rip/jobs'), fetch('/rip/pipeline/items'),
      ]);
      if (drivesResponse.ok) setDriveDashboard(await drivesResponse.json());
      if (jobsResponse.ok) setJobDashboard(await jobsResponse.json());
      if (queueResponse.ok) setPipelineQueue(await queueResponse.json());
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'The saved disc identity could not be forgotten safely.';
      setError(message);
      window.alert(message);
    } finally {
      setControlling(false);
    }
  };

  const cancelRipPlanAndEject = async () => {
    if (!savedJob || !preview) return;
    const drive = driveDashboard?.drives.find((item) => item.current_job_id === savedJob.job_id)
      ?? driveDashboard?.drives.find((item) => item.drive_index === preview.drives[0]?.drive_index);
    if (!drive) {
      setError('The reviewed optical drive is no longer available.');
      return;
    }
    if (!window.confirm(`Cancel this uncompleted rip plan and eject optical drive ${drive.drive_index + 1}? Existing MKVs and partial attempts will be preserved. This is refused if MakeMKV is active for this disc.`)) return;
    setControlling(true);
    setError('');
    try {
      const cancellableStates = new Set(['awaiting_review', 'authorized', 'queued', 'running', 'pause_requested', 'paused', 'failed']);
      const sameDriveJobs = (jobDashboard?.jobs || [savedJob]).filter((job) =>
        cancellableStates.has(job.state)
        && job.preview?.drives.some((plannedDrive) => plannedDrive.drive_index === drive.drive_index)
      );
      if (!sameDriveJobs.some((job) => job.job_id === savedJob.job_id)) sameDriveJobs.push(savedJob);
      let selectedPayload: OrchestrationJob | null = null;
      for (const job of sameDriveJobs) {
        const response = await fetch(`/rip/jobs/${job.job_id}/cancel`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `cancel-${crypto.randomUUID()}` },
          body: JSON.stringify({ confirm_control: true }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : `Rip plan ${job.job_id} could not be cancelled safely.`);
        if (job.job_id === savedJob.job_id) selectedPayload = payload;
      }
      if (selectedPayload) setSavedJob(selectedPayload);
      const ejected = await ejectDrive(drive, true);
      if (!ejected) throw new Error(`The rip plan was cancelled, but optical drive ${drive.drive_index + 1} could not be ejected. Use its Eject disc button to retry.`);
      const jobsResponse = await fetch('/rip/jobs');
      if (jobsResponse.ok) setJobDashboard(await jobsResponse.json());
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'The rip plan could not be cancelled safely.';
      setError(message);
      window.alert(message);
    } finally {
      setControlling(false);
    }
  };

  const controlPipeline = async (action: 'pause' | 'resume', mediaId?: string) => {
    setControlling(true);
    setError('');
    setReviewNotice('');
    try {
    const path = mediaId
      ? `/rip/pipeline/items/${encodeURIComponent(mediaId)}/retry`
      : `/rip/pipeline/${action}`;
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm_control: true }),
    });
    const payload = await response.json();
    if (!response.ok) {
      setError(typeof payload.detail === 'string' ? payload.detail : 'Pipeline control failed safely.');
      return;
    }
    if (mediaId) {
      const refreshed = await fetch('/rip/pipeline/items');
      if (refreshed.ok) setPipelineQueue(await refreshed.json());
    } else {
      setPipelineQueue(payload);
      if (action === 'resume') {
        const selectedItems = payload.items.filter((item: PipelineQueueItem) => queueOnly || Boolean(selectedDiscFingerprint && item.disc_fingerprint === selectedDiscFingerprint));
        const runnable = selectedItems.filter((item: PipelineQueueItem) => item.state === 'queued');
        const held = selectedItems.filter((item: PipelineQueueItem) => item.state === 'review_required');
        const holdCodes = [...new Set(held.map((item: PipelineQueueItem) => item.review_code || 'unspecified_review'))];
        setReviewNotice(runnable.length > 0
          ? `Processing resumed. ${runnable.length} queued item(s) are available to an authorized stage worker.`
          : held.length > 0
            ? `Queue is active, but ${held.length} item(s) require review: ${holdCodes.join(', ')}. Use “Show required choices” below; resume cannot bypass these holds.`
            : 'Processing is active. No unfinished authorized work is waiting.');
      }
    }
    } finally {
      setControlling(false);
    }
  };

  const restartPlaceholderIdentification = async (item: PipelineQueueItem) => {
    if (!window.confirm(`Restart matching for “${item.display_name || item.media_id}” from its verified staged rip? The MKV will be preserved and HandBrake will not run until a real title is identified.`)) return;
    setControlling(true);
    setError('');
    try {
      const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(item.media_id)}/restart-identification`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm_control: true }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Identification could not be restarted safely.');
      const refreshed = await fetch('/rip/pipeline/items');
      if (refreshed.ok) setPipelineQueue(await refreshed.json());
      setReviewNotice('Removed the placeholder episode label and returned the verified rip to matching. No media was changed.');
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'Identification could not be restarted safely.';
      setError(message);
      window.alert(message);
    } finally {
      setControlling(false);
    }
  };

  const restartAllPlaceholderIdentification = async (items: PipelineQueueItem[]) => {
    if (items.length === 0) return;
    if (!window.confirm(`Restart matching for ${items.length} placeholder title(s) from their verified staged rips? The MKVs will be preserved.`)) return;
    setControlling(true);
    setError('');
    try {
      for (const item of items) {
        const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(item.media_id)}/restart-identification`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ confirm_control: true }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : `Identification could not be restarted for ${item.media_id}.`);
      }
      const refreshed = await fetch('/rip/pipeline/items');
      if (refreshed.ok) setPipelineQueue(await refreshed.json());
      setReviewNotice(`Removed ${items.length} placeholder label(s) and returned the verified rips to matching. No media was changed.`);
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'Placeholder identification could not be restarted safely.';
      setError(message);
      window.alert(message);
    } finally {
      setControlling(false);
    }
  };

  const dismissPipelineItems = async (mediaIds: string[]) => {
    if (mediaIds.length === 0) return;
    if (!window.confirm(`Clear ${mediaIds.length} held item(s) from the active queue? This preserves every MKV, partial, contract, log, and history record. It does not delete or move media.`)) return;
    setControlling(true);
    setError('');
    try {
      const response = await fetch('/rip/pipeline/items/dismiss', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ media_ids: mediaIds, confirm_dismiss: true }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Held queue items could not be cleared safely.');
      setPipelineQueue(payload);
      setReviewNotice(`Cleared ${mediaIds.length} held item(s) from the active queue. Media and durable history were preserved.`);
      window.alert(`Cleared ${mediaIds.length} held item(s). No media was deleted.`);
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'Held queue items could not be cleared safely.';
      setError(message);
      window.alert(message);
    } finally {
      setControlling(false);
    }
  };

  const cancelQueuedPipelineItems = async (mediaIds: string[]) => {
    if (mediaIds.length === 0) return;
    if (!window.confirm(`Remove ${mediaIds.length} waiting item(s) from the downstream queue? Every staged MKV, encode, contract, partial, log, and history record will be kept. No media will be deleted or moved.`)) return;
    setControlling(true);
    setError('');
    try {
      const response = await fetch('/rip/pipeline/items/cancel-queued', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ media_ids: mediaIds, confirm_cancel: true }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Waiting items could not be removed safely.');
      setPipelineQueue(payload);
      setReviewNotice(`Removed ${mediaIds.length} waiting item(s) from the active queue. All media and durable history were preserved.`);
      window.alert(`Removed ${mediaIds.length} waiting item(s) from the queue. No media was deleted.`);
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'Waiting items could not be removed safely.';
      setError(message);
      window.alert(message);
    } finally {
      setControlling(false);
    }
  };

  const deleteQueuedStagedSource = async (item: PipelineQueueItem) => {
    const title = item.display_name || item.media_id;
    const futureSkip = item.likely_removable;
    const futureMessage = futureSkip
      ? ' RipWeaver will also remember this exact disc fingerprint and title index so it is skipped on future rips. That decision can be restored from a future disc review.'
      : '';
    if (!window.confirm(`Permanently delete the verified staged rip for “${title}”? This does not change Jellyfin.${futureMessage} The MKV deletion cannot be undone.`)) return;
    setControlling(true);
    setError('');
    try {
      const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(item.media_id)}/delete-staged-source`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          confirm_delete: true,
          remember_future_skip: futureSkip,
        }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'The staged rip could not be deleted safely.');
      if (!Array.isArray(payload.items) || typeof payload.paused !== 'boolean') {
        throw new Error('The staged rip was handled, but the refreshed queue response was invalid. Refresh the page to confirm its state.');
      }
      setPipelineQueue(payload as unknown as PipelineQueue);
      setReviewNotice(futureSkip
        ? `Deleted the staged rip for “${title}” and saved an exact future-rip skip for this disc title.`
        : `Permanently deleted the staged rip for “${title}” and removed it from the active queue. Jellyfin was not changed.`);
      window.alert(futureSkip
        ? `Deleted the staged rip for “${title}” and saved the future-rip skip. Jellyfin was not changed.`
        : `Deleted the staged rip for “${title}”. Reripping will be required to recover it. Jellyfin was not changed.`);
      const jobsResponse = await fetch('/rip/jobs');
      if (jobsResponse.ok) setJobDashboard(await jobsResponse.json());
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'The staged rip could not be deleted safely.';
      setError(message);
      window.alert(message);
    } finally {
      setControlling(false);
    }
  };

  const restoreSkippedDiscTitle = async (
    skipped: RipPreview['skipped_titles'][number],
    driveOverride?: DriveSlot,
    jobOverride?: OrchestrationJob,
  ) => {
    const drive = driveOverride ?? selectedDriveSlot;
    const sourceJob = jobOverride ?? savedJob;
    if (!drive || !sourceJob) {
      setError('The inserted disc is no longer available. Refresh the drives and try again.');
      return;
    }
    if (!window.confirm(`Restore title ${skipped.title_index} to future rip plans and run a new read-only inventory for this disc? This does not rip or change media.`)) return;
    setControlling(true);
    setError('');
    try {
      const response = await fetch('/rip/pipeline/disc-title-dispositions/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          disc_fingerprint: skipped.disc_fingerprint,
          title_index: skipped.title_index,
          confirm_restore: true,
        }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'The future-rip skip could not be restored.');
      if (sourceJob.state === 'awaiting_review') {
        const cancelResponse = await fetch(`/rip/jobs/${sourceJob.job_id}/cancel`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': `restore-title-${crypto.randomUUID()}`,
          },
          body: JSON.stringify({ confirm_control: true }),
        });
        const cancelPayload = await responsePayload(cancelResponse);
        if (!cancelResponse.ok) throw new Error(typeof cancelPayload.detail === 'string' ? cancelPayload.detail : 'The old disc review could not be retired.');
      }
      setSavedJob(null);
      setPreview(null);
      setReviewNotice(`Restored title ${skipped.title_index}. Creating a fresh read-only disc review.`);
      await runPreparedDrive(drive, getDiscSetup(driveSetupKey(drive)));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'The future-rip skip could not be restored.');
    } finally {
      setControlling(false);
    }
  };

  const skipDiscTitleAfterReadFailure = async (discFingerprint: string, titleIndex: number) => {
    if (!window.confirm(`Exclude title ${titleIndex} only for this exact disc fingerprint from future rerips? It is included by default. The current MakeMKV attempt will continue unchanged, no files will be deleted, and this decision can be restored later.`)) return;
    setControlling(true);
    setError('');
    try {
      const response = await fetch('/rip/pipeline/disc-title-dispositions/skip', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          disc_fingerprint: discFingerprint,
          title_index: titleIndex,
          reason: 'repeated_read_failure',
          confirm_skip: true,
        }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'The future-rip skip could not be saved.');
      if (!Array.isArray(payload.items) || !Array.isArray(payload.title_dispositions)) {
        throw new Error('The skip was saved, but the refreshed queue response was invalid. Refresh the page to confirm its state.');
      }
      setPipelineQueue(payload as unknown as PipelineQueue);
      setReviewNotice(`You chose to exclude title ${titleIndex} from future rerips for this exact disc. The current MakeMKV attempt was not changed.`);
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'The future-rip skip could not be saved.';
      setError(message);
      window.alert(message);
    } finally {
      setControlling(false);
    }
  };

  const deleteStagedFilesAlreadyInJellyfin = async () => {
    if (!savedJob) return;
    setControlling(true);
    setError('');
    try {
      const previewResponse = await fetch(`/rip/jobs/${savedJob.job_id}/staged-library-duplicates/preview`, { method: 'POST' });
      const plan = await previewResponse.json();
      if (!previewResponse.ok) throw new Error(typeof plan.detail === 'string' ? plan.detail : 'Staged-file cleanup could not be reviewed safely.');
      if (!plan.file_count) {
        throw new Error('No staged MKVs are eligible. RipWeaver only deletes a staged file when its previously matched Jellyfin file still exists.');
      }
      if (!window.confirm(`Permanently delete exactly ${plan.file_count} staged MKV file(s) (${formatBytes(plan.total_size_bytes)}) because their previously matched Jellyfin files still exist? Jellyfin is not changed. This cannot be undone; reripping will be required to recover these staged copies.`)) return;
      const response = await fetch(`/rip/jobs/${savedJob.job_id}/staged-library-duplicates/delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_plan_sha256: plan.plan_sha256,
          authorized_file_count: plan.file_count,
          confirm_delete: true,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'The reviewed staged files could not be deleted safely.');
      setReviewNotice(`Deleted ${payload.deleted_file_count} redundant staged MKV(s). Existing Jellyfin files were not changed.`);
      window.alert(`Deleted ${payload.deleted_file_count} staged MKV(s). Jellyfin was not changed.`);
      const refreshed = await fetch(`/rip/jobs/${savedJob.job_id}`);
      if (refreshed.ok) {
        const refreshedJob = await refreshed.json();
        setSavedJob(refreshedJob);
        if (refreshedJob.preview) setPreview(refreshedJob.preview);
      }
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'Staged-file cleanup failed safely.';
      setError(message);
      window.alert(message);
    } finally {
      setControlling(false);
    }
  };

  const chooseAmbiguityResolution = async (mediaId: string, choice: 'gemini' | 'manual' | 'hold') => {
    if (choice === 'gemini' && !window.confirm('Use Gemini only after local catalogue, subtitle, OCR, and transcription evidence is exhausted? Only selected short evidence and allowed candidate names may be sent. No MKV, local path, credential, or full transcript is transmitted.')) return;
    setReviewingItemId(mediaId);
    setError('');
    if (choice === 'gemini') setGeminiProgress((current) => ({ ...current, [mediaId]: 'Submitting retry…' }));
    try {
      if (choice === 'gemini') {
        const executionResponse = await fetch('/rip/pipeline/gemini/execute', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ media_ids: [mediaId], confirm_media_read: true, confirm_external_transmission: true }),
        });
        const execution = await responsePayload(executionResponse);
        if (!executionResponse.ok) throw new Error(typeof execution.detail === 'string' ? execution.detail : 'Gemini evidence processing could not be queued.');
        setGeminiProgress((current) => ({ ...current, [mediaId]: 'Collecting local evidence and preparing the Gemini request…' }));
      } else {
        const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(mediaId)}/ambiguity-choice`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ choice, confirm_external_fallback: false }),
        });
        const payload = await responsePayload(response);
        if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'The ambiguity choice could not be saved.');
      }
      const queueResponse = await fetch('/rip/pipeline/items');
      if (queueResponse.ok) setPipelineQueue(await queueResponse.json());
      setReviewNotice(choice === 'gemini'
        ? 'Queued local audio evidence and the confirmed Gemini fallback. Matching will restart automatically for confident results.'
        : choice === 'manual'
          ? 'Manual naming selected. This title remains held until a reviewed feature name is assigned.'
          : 'This title remains safely on hold.');
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'The ambiguity choice could not be saved.';
      setError(message);
      if (choice === 'gemini') setGeminiProgress((current) => ({ ...current, [mediaId]: `Retry did not start: ${message}` }));
    } finally {
      setReviewingItemId(null);
    }
  };

  const resolveLibraryCollision = async (item: PipelineQueueItem, action: 'replace-library' | 'delete-new') => {
    const replacing = action === 'replace-library';
    const message = replacing
      ? item.stage === 'organize'
        ? `Replace the existing Jellyfin episode with the verified encode for “${item.display_name || item.media_id}”? The old Jellyfin file will first be moved to staging-for-deletion for recovery. The raw rip is preserved.`
        : `Authorize replacement for “${item.display_name || item.media_id}”? It will be encoded first, then held for a final verified replacement. The existing Jellyfin file is not changed by this step.`
      : `Permanently delete the new ${item.stage === 'organize' ? 'verified encode' : 'ripped MKV'} for “${item.display_name || item.media_id}”? The existing Jellyfin file is not changed.${item.stage === 'organize' ? ' The raw rip is preserved for reprocessing.' : ' Reripping the disc will be required to recover it.'}`;
    if (!window.confirm(message)) return;
    setControlling(true); setError(''); setReviewNotice('');
    try {
      const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(item.media_id)}/resolve-library-collision`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, expected_artifact_sha256: item.artifact_sha256, confirm_resolution: true }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'The collision choice could not be applied safely.');
      const refreshed = await fetch('/rip/pipeline/items');
      if (refreshed.ok) setPipelineQueue(await refreshed.json());
      setReviewNotice(replacing
        ? item.stage === 'organize' ? 'Verified replacement completed; the previous Jellyfin file was retained in staging-for-deletion.' : 'Replacement authorized. Review and start the queued HandBrake job.'
        : 'The selected new pipeline media was permanently deleted; the existing Jellyfin file was unchanged.');
      window.alert(replacing ? 'Replacement choice applied.' : 'The selected new pipeline media was deleted and duplicate held records were retired.');
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'The collision choice could not be applied safely.';
      setError(message);
      window.alert(message);
    }
    finally { setControlling(false); }
  };

  const inspectLibraryCollision = async (item: PipelineQueueItem) => {
    if (!window.confirm(`Read container metadata from the exact new encode and conflicting Jellyfin file for “${item.display_name || item.media_id}”? This uses local FFprobe and does not modify either file.`)) return;
    setInspectingCollisionId(item.media_id);
    setCollisionComparisonErrors((current) => ({ ...current, [item.media_id]: '' }));
    try {
      const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(item.media_id)}/library-collision-comparison`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_artifact_sha256: item.artifact_sha256, confirm_media_read: true }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'The file comparison could not be completed safely.');
      setCollisionComparisons((current) => ({ ...current, [item.media_id]: payload as unknown as LibraryCollisionComparison }));
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'The file comparison could not be completed safely.';
      setCollisionComparisonErrors((current) => ({ ...current, [item.media_id]: message }));
    } finally {
      setInspectingCollisionId(null);
    }
  };

  const playLibraryCollision = async (item: PipelineQueueItem, target: 'new-encode' | 'existing-jellyfin') => {
    const label = target === 'new-encode' ? 'new verified encode' : 'existing Jellyfin file';
    if (!window.confirm(`Open the exact ${label} for “${item.display_name || item.media_id}” in the Windows default media player? No file will be changed.`)) return;
    setOpeningReviewId(`${item.media_id}:${target}`);
    setError('');
    try {
      const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(item.media_id)}/play-library-collision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target, expected_artifact_sha256: item.artifact_sha256, confirm_play: true }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Collision playback could not start.');
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'Collision playback could not start.';
      setError(message);
      window.alert(message);
    } finally {
      setOpeningReviewId(null);
    }
  };

  const playReview = async (mediaId: string) => {
    if (!window.confirm('Open this exact recorded MKV in the Windows default media player for review? No file will be changed.')) return;
    setOpeningReviewId(mediaId);
    setError('');
    try {
      const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(mediaId)}/play-review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm_play: true }) });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Review playback could not start.');
      setReviewPlaybackOpened((current) => new Set(current).add(mediaId));
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'Review playback could not start.';
      setError(message);
      window.alert(message);
    } finally {
      setOpeningReviewId(null);
    }
  };

  const analyzeSilentVideo = async (item: PipelineQueueItem) => {
    if (!window.confirm(`Read six bounded frames from "${item.display_name || item.media_id}" and run local OCR? The MKV will not be changed or deleted.`)) return;
    setReviewingItemId(item.media_id);
    setSilentVideoOcrRunningId(item.media_id);
    setError('');
    setSilentVideoOcrErrors((current) => ({ ...current, [item.media_id]: '' }));
    try {
      const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(item.media_id)}/silent-video-ocr`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_artifact_sha256: item.artifact_sha256, confirm_media_read: true }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Silent-video OCR review failed safely.');
      setSilentVideoOcr((current) => ({ ...current, [item.media_id]: payload as unknown as SilentVideoOcrResult }));
      const refreshed = await fetch('/rip/pipeline/items');
      if (refreshed.ok) setPipelineQueue(await refreshed.json());
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'Silent-video OCR review failed safely.';
      setError(message);
      setSilentVideoOcrErrors((current) => ({ ...current, [item.media_id]: message }));
    } finally {
      setSilentVideoOcrRunningId(null);
      setReviewingItemId(null);
    }
  };

  const saveManualEpisodeIdentification = async (
    item: PipelineQueueItem,
    reviewedName?: string,
    evidenceSource: 'manual_playback' | 'catalogue_candidate' | 'provider_candidate' = 'manual_playback',
  ) => {
    const newName = (reviewedName || renameDrafts[item.media_id] || '').trim();
    const isBonus = evidenceSource === 'catalogue_candidate' ? false : Boolean(bonusReviewModes[item.media_id]);
    if (!newName) return;
    const destinationSummary = isBonus
      ? `the canonical series Extras folder as "${newName}.mkv"`
      : `the reviewed episode identity "${newName}.mkv"`;
    const provenanceNotice = evidenceSource === 'catalogue_candidate'
      ? ' This remains server-assisted evidence, so it cannot vote toward catalogue consensus or earn a contribution credit.'
      : evidenceSource === 'provider_candidate'
        ? ' This records your explicit choice among the displayed provider candidates.'
      : '';
    if (!window.confirm(`Use ${destinationSummary}? RipWeaver will preserve the original staged file, remember this title for the disc fingerprint, and continue the pipeline.${provenanceNotice}`)) return;
    setReviewingItemId(item.media_id);
    setError('');
    try {
      const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(item.media_id)}/manual-episode-identification`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_name: newName, content_type: isBonus ? 'bonus' : 'episode', evidence_source: evidenceSource, confirm_identification: true }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Manual episode identification could not be saved.');
      setRenameDrafts((current) => ({ ...current, [item.media_id]: '' }));
      setBonusReviewModes((current) => ({ ...current, [item.media_id]: false }));
      const refreshed = await fetch('/rip/pipeline/items');
      if (refreshed.ok) setPipelineQueue(await refreshed.json());
      setReviewNotice(evidenceSource === 'catalogue_candidate'
        ? 'Accepted the single-upload community candidate as server-assisted evidence. It will continue locally but cannot reinforce its own catalogue vote.'
        : isBonus
          ? 'Saved the reviewed TV bonus identity. It will use the canonical series Extras folder in Jellyfin.'
          : 'Saved the reviewed episode identity and returned the staged rip to the automatic pipeline. The .mkv extension is preserved.');
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'Manual episode identification could not be saved.';
      setError(message);
      window.alert(message);
    } finally {
      setReviewingItemId(null);
    }
  };

  const renameProvisional = async (mediaId: string) => {
    const newName = (renameDrafts[mediaId] || '').trim(); if (!newName) return;
    if (!window.confirm(`Rename this Jellyfin file to “${newName}.mkv”? The .mkv extension is preserved and existing files will not be overwritten.`)) return;
    const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(mediaId)}/rename-provisional`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ new_name: newName, confirm_rename: true }) });
    const payload = await response.json(); if (!response.ok) setError(payload.detail || 'Provisional file could not be renamed.'); else setRenameDrafts((current) => ({ ...current, [mediaId]: '' }));
  };

  const reviewTranscodeAuthorization = async () => {
    setError('');
    const selectedProfile = transcodeProfile || 'auto-resolution';
    const query = selectedProfile === 'auto-resolution' ? '' : `?profile_id=${encodeURIComponent(selectedProfile)}`;
    const response = await fetch(`/rip/pipeline/transcode/preview${query}`);
    const payload = await response.json();
    if (!response.ok) {
      const message = typeof payload.detail === 'string' ? payload.detail : 'Transcode authorization could not be prepared safely.';
      setError(message);
      window.alert(message);
      return;
    }
    setTranscodePlan(payload);
  };

  const authorizeTranscode = async () => {
    if (!transcodePlan) return;
    if (!window.confirm(`Start HandBrake for exactly ${transcodePlan.item_count} queued item(s) using ${transcodePlan.profile_display_name}? Outputs go only to encoded staging. Existing files are never overwritten, and nothing will be organized into Jellyfin.`)) return;
    setControlling(true);
    setError('');
    try {
      const response = await fetch('/rip/pipeline/transcode/authorize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_plan_sha256: transcodePlan.plan_sha256,
          authorized_item_count: transcodePlan.item_count,
          profile_id: transcodePlan.profile_selection === 'source-resolution' ? null : transcodePlan.default_profile_id,
          confirm_transcode: true,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Transcode authorization failed safely.');
      setReviewNotice(`Started the reviewed ${payload.authorized_item_count}-item transcode batch. Organization remains held.`);
      setTranscodePlan(null);
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'Transcode authorization failed safely.';
      setError(message);
      window.alert(message);
    } finally {
      setControlling(false);
    }
  };

  const reviewOrganizationAuthorization = async () => {
    setError('');
    const response = await fetch('/rip/pipeline/organization/preview');
    const payload = await response.json();
    if (!response.ok) {
      setError(typeof payload.detail === 'string' ? payload.detail : 'Jellyfin organization review could not be prepared safely.');
      return;
    }
    setOrganizationPlan(payload);
  };

  const authorizeOrganization = async () => {
    if (!organizationPlan) return;
    if (organizationPlan.collision_count > 0) {
      setError('One or more Jellyfin destinations already exist. Those items require collision review and will not be overwritten.');
      return;
    }
    if (!window.confirm(`Move exactly ${organizationPlan.item_count} verified encoded file(s) from staging into the configured Jellyfin libraries? Existing destinations will not be overwritten. This removes each successfully moved file from encoded staging.`)) return;
    setControlling(true);
    setError('');
    try {
      const response = await fetch('/rip/pipeline/organization/authorize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_plan_sha256: organizationPlan.plan_sha256,
          authorized_item_count: organizationPlan.item_count,
          confirm_organize: true,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Jellyfin organization authorization failed safely.');
      setReviewNotice(`Started the reviewed ${payload.authorized_item_count}-item Jellyfin placement batch. No overwrite was authorized.`);
      setOrganizationPlan(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Jellyfin organization authorization failed safely.');
    } finally {
      setControlling(false);
    }
  };

  const reripMissingItems = async (job: OrchestrationJob, exactTitleIndexes?: number[]) => {
    const titleCount = job.preview?.jobs.length ?? 0;
    const selectedTitleIndexes = [...new Set(
      exactTitleIndexes
      ?? job.rip_title_summary?.unfinished_titles.map((title) => title.title_index)
      ?? job.preview?.jobs.map((title) => title.title_index)
      ?? [],
    )].sort((left, right) => left - right);
    const unfinishedCount = selectedTitleIndexes.length;
    if (!job.preview || titleCount === 0) {
      setError('The incomplete rip no longer has an exact reviewed title plan.');
      return;
    }
    if (unfinishedCount === 0) {
      setError('Every reviewed title from this failed rip is already safely present in staging or Jellyfin.');
      return;
    }
    const selectedTitleDescription = unfinishedCount === 1
      ? `title index ${selectedTitleIndexes[0]}`
      : `title indexes ${selectedTitleIndexes.join(', ')}`;
    setRerippingJobId(job.job_id);
    setError('');
    try {
      let cleanup: FailedRipCleanupPlan | null = null;
      if (!['failed', 'awaiting_review'].includes(job.state)) {
        const cleanupResponse = await fetch(`/rip/jobs/${job.job_id}/failed-attempts`);
        const cleanupPayload = await cleanupResponse.json();
        if (!cleanupResponse.ok) throw new Error(typeof cleanupPayload.detail === 'string' ? cleanupPayload.detail : 'Interrupted output could not be reviewed safely.');
        cleanup = cleanupPayload as FailedRipCleanupPlan;
        setFailedCleanupPlan(cleanup);
      }
      const cleanupRequired = (cleanup?.attempt_directory_count ?? 0) > 0;
      const cleanupDescription = cleanupRequired
        ? ` remove ${cleanup?.attempt_directory_count} exact interrupted-attempt folder(s) containing ${cleanup?.file_count} incomplete MKV file(s) (${formatBytes(cleanup?.total_bytes ?? 0)}), then`
        : job.state === 'failed'
          ? ' preserve the failed attempt for review, then'
          : '';
      if (!window.confirm(
        `Rerip exactly ${unfinishedCount} missing ${unfinishedCount === 1 ? 'title' : 'titles'} now (${selectedTitleDescription})? RipWeaver will${cleanupDescription} start MakeMKV for only these ${unfinishedCount} selected ${unfinishedCount === 1 ? 'title' : 'titles'} in a new isolated attempt. Existing staged titles and Jellyfin files will be preserved and never overwritten.`,
      )) return;
      let recovered = job;
      if (job.state === 'failed' || job.state === 'awaiting_review') {
        const selectionResponse = await fetch(`/rip/jobs/${job.job_id}/select-titles`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': `select-missing-${crypto.randomUUID()}`,
          },
          body: JSON.stringify({ title_indexes: selectedTitleIndexes, confirm_selection: true }),
        });
        const selectionPayload = await selectionResponse.json();
        if (!selectionResponse.ok) throw new Error(typeof selectionPayload.detail === 'string' ? selectionPayload.detail : 'The exact missing-title rerip plan could not be created safely.');
        recovered = selectionPayload as OrchestrationJob;
        const authorizeResponse = await fetch(`/rip/jobs/${recovered.job_id}/authorize`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': `authorize-missing-${crypto.randomUUID()}`,
          },
          body: JSON.stringify({
            expected_plan_sha256: recovered.plan_sha256,
            confirm_authorization: true,
          }),
        });
        const authorizePayload = await authorizeResponse.json();
        if (!authorizeResponse.ok) throw new Error(typeof authorizePayload.detail === 'string' ? authorizePayload.detail : 'The exact missing-title rerip plan could not be authorized safely.');
        recovered = authorizePayload as OrchestrationJob;
        const startResponse = await fetch(`/rip/jobs/${recovered.job_id}/start`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': `queue-missing-${crypto.randomUUID()}`,
          },
          body: JSON.stringify({ confirm_queue: true }),
        });
        const startPayload = await startResponse.json();
        if (!startResponse.ok) throw new Error(typeof startPayload.detail === 'string' ? startPayload.detail : 'The exact missing-title rerip could not be queued safely.');
        recovered = startPayload as OrchestrationJob;
      } else if (job.state === 'paused') {
        const resumeResponse = await fetch(`/rip/jobs/${job.job_id}/resume`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': `resume-${crypto.randomUUID()}`,
          },
          body: JSON.stringify({ confirm_control: true }),
        });
        const resumePayload = await resumeResponse.json();
        if (!resumeResponse.ok) throw new Error(typeof resumePayload.detail === 'string' ? resumePayload.detail : 'Interrupted rip could not be resumed safely.');
        recovered = resumePayload as OrchestrationJob;
      } else if (job.state !== 'queued') {
        throw new Error('Only a reviewed, failed, paused, or already queued rip can rerip unfinished titles.');
      }
      setSavedJob(recovered);
      if (recovered.preview) setPreview(recovered.preview);
      setJobDashboard((current) => current ? {
        ...current,
        jobs: [recovered, ...current.jobs.filter((candidate) => candidate.job_id !== recovered.job_id)],
      } : current);
      setReviewNotice(`Queued the exact ${unfinishedCount}-title rerip. RipWeaver is waiting for the execution boundary to become available.`);

      const executeResponse = await fetch(`/rip/jobs/${recovered.job_id}/execute`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': `execute-resume-${crypto.randomUUID()}`,
        },
        body: JSON.stringify({
          expected_plan_sha256: recovered.plan_sha256,
          authorized_job_count: recovered.preview?.jobs.length ?? unfinishedCount,
          timeout_seconds: 7200,
          max_drives: recovered.preview?.drives.length ?? job.preview.drives.length,
          confirm_execute: true,
          preserve_failed_partials: !cleanupRequired,
          failed_cleanup_sha256: cleanupRequired ? cleanup?.plan_sha256 : null,
          confirm_failed_cleanup: cleanupRequired,
        }),
      });
      const executed = await executeResponse.json();
      if (!executeResponse.ok) throw new Error(typeof executed.detail === 'string' ? executed.detail : 'Interrupted rip could not be restarted safely.');
      setSavedJob(executed);
      setPreview(executed.preview);
      setReviewNotice(cleanupRequired
        ? `Removed ${cleanup?.attempt_directory_count} confirmed interrupted-attempt folder(s) and started the ${unfinishedCount}-item rerip. Verified titles are in the identification queue; Jellyfin files were preserved.`
        : `Started the ${unfinishedCount}-item rerip in a new isolated attempt. Verified titles are in the identification queue.`);
      const jobsResponse = await fetch('/rip/jobs');
      if (jobsResponse.ok) setJobDashboard(await jobsResponse.json());
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'Missing-title rerip failed safely.';
      setError(message);
      window.alert(`The missing-title rerip did not start. ${message}`);
    } finally {
      setRerippingJobId(null);
    }
  };

  const runPreparedDrive = async (
    drive: DriveSlot,
    setup: DiscSetup,
    catalogueLookupMode: 'automatic' | 'manual' = 'automatic',
  ): Promise<OrchestrationJob | null> => {
    setPreparingDrive(drive.drive_index);
    setError('');
    try {
      const response = await fetch('/rip/drives/prepare-pipeline', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': `prepare-drive-${drive.drive_index}-${crypto.randomUUID()}`,
        },
        body: JSON.stringify({
          drive_index: drive.drive_index,
          content_hint: setup.contentType || null,
          handbrake_profile_id: setup.handbrakeProfile || null,
          library_policy: setup.addMissingOnly ? 'missing-only' : 'review-conflicts',
          catalogue_lookup_mode: catalogueLookupMode,
          support_prompt_version: catalogueLookupMode === 'manual'
            ? catalogueSupportStatus?.policy?.terms_version ?? '2026-08-10'
            : null,
          confirm_read: true,
          timeout_seconds: 300,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Disc pipeline could not be prepared safely.');
      if (catalogueLookupMode === 'manual' && payload?.preview) {
        setSavedJob(payload as OrchestrationJob);
        setPreview(payload.preview as RipPreview);
        setReviewNotice('The manual catalogue lookup completed. Review the refreshed disc plan below.');
      }
      const jobsResponse = await fetch('/rip/jobs');
      if (jobsResponse.ok) setJobDashboard(await jobsResponse.json());
      return payload as OrchestrationJob;
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Disc pipeline could not be prepared safely.');
      return null;
    } finally {
      setPreparingDrive(null);
      setQueuedPrepareDrives((current) => current.filter((index) => index !== drive.drive_index));
    }
  };

  const queueDrivePipeline = (drive: DriveSlot, setup: DiscSetup) => {
    const warning = 'Queue this disc for a read-only MakeMKV inventory? Preparation scans run one drive at a time. It creates a reviewable plan but does not rip, transcode, rename, move, delete, or eject anything.';
    if (!window.confirm(warning)) return;
    setQueuedPrepareDrives((current) => current.includes(drive.drive_index) ? current : [...current, drive.drive_index]);
    prepareQueue.current = prepareQueue.current
      .catch(() => undefined)
      .then(async () => { await runPreparedDrive(drive, setup); });
  };

  const refreshFailedDiscRecovery = async (drive: DriveSlot) => {
    if (!window.confirm(`Read optical drive ${drive.drive_index + 1} again and replace the stale failed-disc recovery plan with the exact currently verifiable relevant title scope? This is a read-only MakeMKV inventory and will not start ripping or modify media.`)) return;
    setReviewNotice(`Reading optical drive ${drive.drive_index + 1} and rebuilding the failed-disc recovery scope…`);
    const refreshed = await runPreparedDrive(drive, getDiscSetup(driveSetupKey(drive)));
    if (!refreshed?.preview) {
      window.alert('The failed-disc recovery refresh did not complete. Review the error shown in RipWeaver, then try again. No rip was started.');
      return;
    }
    setSavedJob(refreshed);
    setPreview(refreshed.preview);
    setReviewNotice(`Prepared a fresh failed-disc recovery containing ${refreshed.preview.jobs.length} relevant reviewed title${refreshed.preview.jobs.length === 1 ? '' : 's'}. Review the exact missing set before starting MakeMKV.`);
  };

  const continueCatalogueLookupManually = () => {
    if (!selectedDriveSlot) {
      setError('The disc must remain in its optical drive to continue the manual lookup.');
      return;
    }
    if (!window.confirm('Continue this catalogue lookup manually? RipWeaver will repeat the read-only disc inventory and will not charge an automatic lookup credit. No rip, transcode, rename, move, delete, or eject operation will begin.')) return;
    setQueuedPrepareDrives((current) => current.includes(selectedDriveSlot.drive_index) ? current : [...current, selectedDriveSlot.drive_index]);
    prepareQueue.current = prepareQueue.current
      .catch(() => undefined)
      .then(async () => {
        await runPreparedDrive(
          selectedDriveSlot,
          getDiscSetup(driveSetupKey(selectedDriveSlot)),
          'manual',
        );
      });
  };

  const beginSupportCheckout = async () => {
    const policy = catalogueSupportStatus?.policy;
    if (!policy?.payments_enabled) {
      setError('RipWeaver support payments are not configured yet. You can continue this lookup manually.');
      return;
    }
    setStartingSupportCheckout(true);
    setError('');
    try {
      const response = await fetch('/catalogue/support/checkout', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': `support-checkout-${crypto.randomUUID()}`,
        },
        body: JSON.stringify({
          amount_cents: supportAmountCents,
          support_rate_cents: supportRateCents,
          terms_version: policy.terms_version,
          accept_best_effort_terms: supportTermsAccepted,
        }),
      });
      const payload = await response.json();
      if (!response.ok || typeof payload.checkout_url !== 'string') {
        throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Support checkout is unavailable.');
      }
      window.open(payload.checkout_url, '_blank', 'noopener,noreferrer');
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Support checkout is unavailable.');
    } finally {
      setStartingSupportCheckout(false);
    }
  };

  const selectDriveJob = (job: OrchestrationJob, focusElementId?: string) => {
    if (!job.preview) return;
    setSavedJob(job);
    setPreview(job.preview);
    setConfirmPhysicalRip(false);
    setError('');
    setReviewNotice('');
    setSelectingTitles(false);
    setSelectedTitleIndexes(job.preview.jobs.map((item) => item.title_index));
    setExistingRecoveryPlan(null);
    if (focusElementId) {
      window.setTimeout(() => {
        const target = document.getElementById(focusElementId);
        target?.scrollIntoView({ behavior: 'smooth', block: 'center' });
        target?.focus({ preventScroll: true });
      }, 100);
    }
    setExistingRipsRestarted(false);
    window.requestAnimationFrame(() => {
      document.getElementById('selected-disc-review')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  };

  const controlJob = async (action: 'authorize' | 'start' | 'execute' | 'pause' | 'stop' | 'return-to-review') => {
    if (!savedJob) return;
    const longRunningExecution = action === 'execute';
    if (longRunningExecution) setExecutingJobId(savedJob.job_id);
    else setControlling(true);
    setError('');
    try {
      let body: Record<string, unknown>;
      if (action === 'authorize') {
        body = { expected_plan_sha256: savedJob.plan_sha256, confirm_authorization: true };
      } else if (action === 'start') {
        body = { confirm_queue: true };
      } else if (action === 'execute') {
        if (!confirmPhysicalRip || !preview) {
          throw new Error('Confirm the reviewed physical rip before starting.');
        }
        let failedCleanupSha256: string | null = null;
        let confirmFailedCleanup = false;
        if (!preserveFailedPartials) {
          const cleanupResponse = await fetch(`/rip/jobs/${savedJob.job_id}/failed-attempts`);
          const cleanup = await cleanupResponse.json();
          if (!cleanupResponse.ok) throw new Error(typeof cleanup.detail === 'string' ? cleanup.detail : 'Failed attempts could not be reviewed safely.');
          setFailedCleanupPlan(cleanup);
          failedCleanupSha256 = cleanup.plan_sha256;
          confirmFailedCleanup = true;
          if (cleanup.file_count > 0 && !window.confirm(
            `Start this rip from the beginning and permanently remove ${cleanup.file_count} incomplete MKV file(s) from ${cleanup.attempt_directory_count} failed attempt folder(s) (${formatBytes(cleanup.total_bytes)})? Verified outputs, Jellyfin files, and unrelated files will not be changed.`,
          )) return;
        }
        body = {
          expected_plan_sha256: savedJob.plan_sha256,
          authorized_job_count: preview.jobs.length,
          timeout_seconds: 7200,
          max_drives: preview.drives.length,
          confirm_execute: true,
          preserve_failed_partials: preserveFailedPartials,
          failed_cleanup_sha256: failedCleanupSha256,
          confirm_failed_cleanup: confirmFailedCleanup,
        };
      } else {
        body = { confirm_control: true };
      }
      const response = await fetch(`/rip/jobs/${savedJob.job_id}/${action}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': `${action}-${crypto.randomUUID()}`,
        },
        body: JSON.stringify(body),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : `${action} failed safely.`);
      setSavedJob(payload);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Control request failed safely.');
    } finally {
      if (longRunningExecution) setExecutingJobId(null);
      else setControlling(false);
    }
  };

  const authorizeAndQueueJob = async (job: OrchestrationJob): Promise<OrchestrationJob> => {
      const authorizeResponse = await fetch(`/rip/jobs/${job.job_id}/authorize`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': `authorize-${crypto.randomUUID()}`,
        },
        body: JSON.stringify({
          expected_plan_sha256: job.plan_sha256,
          confirm_authorization: true,
        }),
      });
      const authorized = await authorizeResponse.json() as OrchestrationJob & { detail?: string };
      if (!authorizeResponse.ok) {
        throw new Error(typeof authorized.detail === 'string' ? authorized.detail : 'The title plan could not be approved safely.');
      }
      const queueResponse = await fetch(`/rip/jobs/${job.job_id}/start`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': `start-${crypto.randomUUID()}`,
        },
        body: JSON.stringify({ confirm_queue: true }),
      });
      const queued = await queueResponse.json() as OrchestrationJob & { detail?: string };
      if (!queueResponse.ok) {
        setSavedJob(authorized);
        throw new Error(typeof queued.detail === 'string' ? queued.detail : 'The approved plan could not be added to the queue safely.');
      }
      return queued;
  };

  const approveAndQueueJob = async () => {
    if (!savedJob) return;
    setControlling(true);
    setError('');
    try {
      let jobToQueue = savedJob;
      const currentDrive = driveDashboard?.drives.find(
        (drive) => drive.current_disc_fingerprint === selectedDiscFingerprint,
      ) ?? (
        driveDashboard?.drives.filter((drive) => drive.has_disc).length === 1
          ? driveDashboard.drives.find((drive) => drive.has_disc)
          : undefined
      );
      const plannedDriveIndex = savedJob.preview?.drives[0]?.drive_index;
      if (currentDrive && typeof plannedDriveIndex === 'number' && currentDrive.drive_index !== plannedDriveIndex) {
        if (!window.confirm(
          `This continuation was saved for optical drive ${plannedDriveIndex + 1}, but the same disc is now in optical drive ${currentDrive.drive_index + 1}. Read the disc now, verify its fingerprint and title inventory, then bind and queue these same ${savedJob.preview?.jobs.length ?? 0} missing titles? This read-only preparation does not start MakeMKV ripping or change media.`,
        )) return;
        setReviewNotice(`Reading optical drive ${currentDrive.drive_index + 1} and safely rebinding the missing-title continuation…`);
        const setup = getDiscSetup(driveSetupKey(currentDrive));
        const prepareResponse = await fetch('/rip/drives/prepare-pipeline', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': `rebind-drive-${currentDrive.drive_index}-${crypto.randomUUID()}`,
          },
          body: JSON.stringify({
            drive_index: currentDrive.drive_index,
            content_hint: setup.contentType || null,
            handbrake_profile_id: setup.handbrakeProfile || null,
            library_policy: setup.addMissingOnly ? 'missing-only' : 'review-conflicts',
            confirm_read: true,
            timeout_seconds: 300,
          }),
        });
        const rebound = await prepareResponse.json() as OrchestrationJob & { detail?: string };
        if (!prepareResponse.ok) {
          throw new Error(typeof rebound.detail === 'string' ? rebound.detail : 'The continuation could not be rebound to the current optical drive.');
        }
        const expectedTitles = savedJob.preview?.jobs.map((item) => item.title_index).sort((left, right) => left - right) ?? [];
        const reboundTitles = rebound.preview?.jobs.map((item) => item.title_index).sort((left, right) => left - right) ?? [];
        if (rebound.preview?.drives[0]?.drive_index !== currentDrive.drive_index || JSON.stringify(reboundTitles) !== JSON.stringify(expectedTitles)) {
          throw new Error('The fresh disc review did not preserve the exact missing-title continuation. Nothing was queued.');
        }
        jobToQueue = rebound;
      }
      const queued = await authorizeAndQueueJob(jobToQueue);
      setSavedJob(queued);
      if (queued.preview) setPreview(queued.preview);
      setReviewNotice(`The exact ${queued.preview?.jobs.length ?? 0}-title continuation is queued. Complete the final physical-rip confirmation below to start MakeMKV.`);
      const jobsResponse = await fetch('/rip/jobs');
      if (jobsResponse.ok) setJobDashboard(await jobsResponse.json());
      window.requestAnimationFrame(() => document.getElementById('rip-execution-confirmation')?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'The title plan could not be queued safely.';
      setError(message);
      setReviewNotice(message);
    } finally {
      setControlling(false);
    }
  };

  const resolveRipCollisions = async (policy: 'missing-only' | 'rerip-all' | 'replace-after-verification') => {
    if (!savedJob) return;
    const message = policy === 'missing-only'
      ? 'Create a new review containing only titles whose planned destinations are absent? Existing files and partial attempts will remain untouched.'
      : policy === 'replace-after-verification'
        ? 'Mark these titles for deliberate replacement? Every title will first be reripped and verified in isolated staging. The UI will require another exact confirmation showing the old and new file before the old completed file is replaced. Partials are never overwritten.'
        : 'Create a new isolated attempt for every selected title? This does not delete or overwrite earlier rips. Replacement of an old completed file is decided only after the new rip verifies.';
    if (!window.confirm(message)) return;
    setControlling(true);
    setError('');
    try {
      const response = await fetch(`/rip/jobs/${savedJob.job_id}/resolve-collisions`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': `resolve-${policy}-${crypto.randomUUID()}`,
        },
        body: JSON.stringify({ policy, confirm_resolution: true }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'The collision choice could not be applied safely.');
      setSavedJob(payload);
      setPreview(payload.preview);
      setReviewNotice('Your choice was saved and this disc was added to the rip queue. Continue to the final physical-rip confirmation when ready.');
      const jobsResponse = await fetch('/rip/jobs');
      if (jobsResponse.ok) setJobDashboard(await jobsResponse.json());
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'The collision choice could not be applied safely.');
    } finally {
      setControlling(false);
    }
  };

  const restartExistingPipeline = async () => {
    if (!savedJob) return;
    const scope = selectingTitles ? `${selectedTitleIndexes.length} checked title(s)` : 'all titles from this disc';
    const allSeasonReady = Boolean(
      selectedDiscFingerprint
      && suggestedUnmatchedSeries
      && visiblePipelineItems.some((item) => (
        item.stage === 'identify'
        && item.state === 'review_required'
        && requiresDiscWideTvRecovery(item)
      )),
    );
    const confirmation = allSeasonReady
      ? `Restart matching for ${scope} and automatically analyze the complete disc as “${suggestedUnmatchedSeries}”? This reads short MKV audio samples locally and queries episode metadata.${automaticGeminiFallback ? ' If local results remain ambiguous, the configured Gemini fallback may receive bounded evidence and candidate episode metadata.' : ''} It does not read the optical disc, rerip, rename, overwrite, delete, or transcode media.`
      : `Reuse verified completed titles or resume a failed rip covering ${scope}? RipWeaver will accept only completed files tied to this disc and leave partial or missing titles for a new rip plan. This does not read the optical disc, rerip, rename, overwrite, or delete media. Files without a durable verified-rip record will remain held for verification.`;
    if (!window.confirm(confirmation)) return;
    setControlling(true);
    setError('');
    setReviewNotice('Checking durable verification records…');
    let shouldPreviewRecovery = false;
    try {
      if (allSeasonReady && selectedDiscFingerprint) {
        const analysisResponse = await fetch('/rip/pipeline/analyze-unmatched-disc', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            disc_fingerprint: selectedDiscFingerprint,
            series_name: suggestedUnmatchedSeries,
            season: suggestedSeason,
            confirm_media_read: true,
            confirm_provider_lookup: true,
            confirm_external_fallback: automaticGeminiFallback,
          }),
        });
        const analysisPayload = await analysisResponse.json();
        if (!analysisResponse.ok) throw new Error(typeof analysisPayload.detail === 'string' ? analysisPayload.detail : 'All-season analysis could not be started.');
        setReviewNotice(`Restarted matching and began all-season analysis for ${analysisPayload.item_count} verified title(s).`);
        setExistingRipsRestarted(true);
        const refreshed = await fetch('/rip/pipeline/items');
        if (refreshed.ok) setPipelineQueue(await refreshed.json());
        return;
      }
      const response = await fetch(`/rip/jobs/${savedJob.job_id}/restart-existing-pipeline`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          confirm_restart: true,
          title_indexes: selectingTitles ? selectedTitleIndexes : null,
        }),
      });
      const payload = await response.json();
      if (!response.ok) {
        shouldPreviewRecovery = response.status === 409;
        throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Existing rips could not be restarted safely.');
      }
      const queueResponse = await fetch('/rip/pipeline/items');
      if (queueResponse.ok) setPipelineQueue(await queueResponse.json());
      setReviewNotice(`Restarted matching for ${payload.restarted_count} verified title(s). ${payload.verification_required_count} title(s) still require verification.`);
      setExistingRipsRestarted(payload.restarted_count > 0);
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'Existing rips could not be restarted safely.';
      setReviewNotice(message);
      setError(message);
      if (shouldPreviewRecovery && savedJob) {
        const previewResponse = await fetch(`/rip/jobs/${savedJob.job_id}/existing-rips/preview`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ confirm_search: true }),
        });
        const recovery = await previewResponse.json();
        if (previewResponse.ok) {
          setExistingRecoveryPlan(recovery);
          const grouped = new Map<number, string[]>();
          recovery.candidates.forEach((item: { title_index: number; candidate_id: string }) => grouped.set(item.title_index, [...(grouped.get(item.title_index) ?? []), item.candidate_id]));
          setSelectedRecoveryCandidates(Object.fromEntries([...grouped].filter(([, ids]) => ids.length === 1).map(([title, ids]) => [title, ids[0]])));
          setReviewNotice(
            recovery.candidates.length > 0
              ? `Found ${recovery.candidates.length} exact staged candidate(s). Review the list below, then confirm read-only FFprobe verification.`
              : 'No exact reusable staged MKVs were found for this disc fingerprint and title selection. No media was read or changed.',
          );
          setError('');
        }
      }
    } finally {
      setControlling(false);
    }
  };

  const verifyExistingCandidates = async () => {
    if (!savedJob || !existingRecoveryPlan) return;
    const candidateIds = Object.values(selectedRecoveryCandidates);
    if (candidateIds.length === 0) return;
    const recoveredTitleIndexes = new Set(Object.keys(selectedRecoveryCandidates).map(Number));
    const missingTitleIndexes = preview?.jobs
      .map((item) => item.title_index)
      .filter((titleIndex) => !recoveredTitleIndexes.has(titleIndex)) ?? [];
    const continuation = missingTitleIndexes.length > 0
      ? ` RipWeaver will then prepare a continuation review for the ${missingTitleIndexes.length} missing title(s): ${missingTitleIndexes.join(', ')}.`
      : '';
    if (!window.confirm(`Run read-only FFprobe verification on ${candidateIds.length} exact staged MKV(s), then queue successful files for matching?${continuation} No file will be changed, renamed, moved, or deleted, and MakeMKV will still require its final confirmation.`)) return;
    setControlling(true);
    setReviewNotice('Verifying selected staged MKVs…');
    try {
      const response = await fetch(`/rip/jobs/${savedJob.job_id}/existing-rips/verify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_plan_sha256: existingRecoveryPlan.plan_sha256,
          candidate_ids: candidateIds,
          timeout_seconds: 60,
          confirm_media_read: true,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Existing-rip verification failed safely.');
      setExistingRipsRestarted(payload.verified_count > 0);
      setExistingRecoveryPlan(null);
      const verifiedIndexes = new Set<number>(payload.verified_title_indexes ?? []);
      const continuationTitleIndexes = preview?.jobs
        .map((item) => item.title_index)
        .filter((titleIndex) => !verifiedIndexes.has(titleIndex)) ?? missingTitleIndexes;
      if (continuationTitleIndexes.length > 0) {
        const continuationResponse = await fetch(`/rip/jobs/${savedJob.job_id}/select-titles`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': `resume-missing-${crypto.randomUUID()}`,
          },
          body: JSON.stringify({
            title_indexes: continuationTitleIndexes,
            confirm_selection: true,
          }),
        });
        const continuationPayload = await continuationResponse.json();
        if (!continuationResponse.ok) throw new Error(typeof continuationPayload.detail === 'string' ? continuationPayload.detail : 'The missing-title continuation could not be prepared.');
        const currentDrive = driveDashboard?.drives.find(
          (drive) => drive.current_disc_fingerprint === selectedDiscFingerprint,
        );
        const plannedDriveIndex = continuationPayload.preview?.drives[0]?.drive_index;
        const driveBindingChanged = currentDrive
          && typeof plannedDriveIndex === 'number'
          && currentDrive.drive_index !== plannedDriveIndex;
        const queuedContinuation = driveBindingChanged
          ? continuationPayload
          : await authorizeAndQueueJob(continuationPayload);
        setSavedJob(queuedContinuation);
        setPreview(continuationPayload.preview);
        setExistingRipsRestarted(false);
        setSelectedTitleIndexes(continuationTitleIndexes);
        setSelectingTitles(false);
        const rejectedNotice = (payload.rejected_title_indexes?.length ?? 0) > 0
          ? ` ${payload.rejected_title_indexes.length} candidate(s) did not pass FFprobe and were returned to the rip continuation.`
          : '';
        if (driveBindingChanged) {
          setReviewNotice(`Verified ${payload.verified_count} existing MKV(s).${rejectedNotice} The missing-title continuation still names optical drive ${plannedDriveIndex + 1}, but this disc is now in optical drive ${currentDrive.drive_index + 1}. Read the disc again to create a fresh, safely bound continuation; RipWeaver will not queue the stale drive binding.`);
        } else {
          setReviewNotice(`Verified ${payload.verified_count} existing MKV(s).${rejectedNotice} Prepared and queued the exact continuation for ${continuationTitleIndexes.length} missing title(s). Complete the final physical-rip confirmation below to start MakeMKV.`);
          window.requestAnimationFrame(() => document.getElementById('rip-execution-confirmation')?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
        }
      } else {
        setReviewNotice(`Verified ${payload.verified_count} existing MKV(s) and queued them for identification. No titles remain to rip.`);
      }
      const queueResponse = await fetch('/rip/pipeline/items');
      if (queueResponse.ok) setPipelineQueue(await queueResponse.json());
      const jobsResponse = await fetch('/rip/jobs');
      if (jobsResponse.ok) setJobDashboard(await jobsResponse.json());
    } catch (requestError) {
      setReviewNotice(requestError instanceof Error ? requestError.message : 'Existing-rip verification failed safely.');
    } finally {
      setControlling(false);
    }
  };

  const createSelectedTitleReview = async () => {
    if (!savedJob || selectedTitleIndexes.length === 0) return;
    setControlling(true);
    setReviewNotice('Creating an exact review for the checked titles…');
    try {
      const response = await fetch(`/rip/jobs/${savedJob.job_id}/select-titles`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': `select-titles-${crypto.randomUUID()}`,
        },
        body: JSON.stringify({
          title_indexes: selectedTitleIndexes,
          confirm_selection: true,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Selected-title review could not be created.');
      setSavedJob(payload);
      setPreview(payload.preview);
      setSelectedTitleIndexes(payload.preview.jobs.map((item: PreviewJob) => item.title_index));
      setReviewNotice(`Created a new review containing ${payload.preview.jobs.length} checked title(s).`);
    } catch (requestError) {
      setReviewNotice(requestError instanceof Error ? requestError.message : 'Selected-title review could not be created.');
    } finally {
      setControlling(false);
    }
  };

  const keepReviewOnHold = () => {
    setSavedJob(null);
    setPreview(null);
    setError('');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  // Bind a drive card only to the process-local job recorded for its current
  // disc. A reusable tray index must never select an older disc's saved job.
  const latestJobForDrive = useCallback((driveIndex: number) => {
    const drive = driveDashboard?.drives.find((item) => item.drive_index === driveIndex);
    // An attached executor is the strongest live binding available. During a
    // MakeMKV run, a transient watcher refresh can temporarily lose the cached
    // fingerprint/current-job fields; falling back to this exact active drive
    // restores progress without selecting an old inactive job for a reused tray.
    const attached = (jobDashboard?.jobs ?? []).find((candidate) => (
      candidate.executor_attached
      && ['running', 'pause_requested'].includes(candidate.state)
      && candidate.preview?.drives.some((item) => item.drive_index === driveIndex)
    ));
    if (attached) return attached;
    const currentJobId = drive?.current_job_id;
    const bound = currentJobId
      ? (jobDashboard?.jobs ?? []).find((candidate) => candidate.job_id === currentJobId)
      : undefined;
    if (bound && ['running', 'pause_requested'].includes(bound.state)) return bound;
    const currentFingerprint = drive?.current_disc_fingerprint;
    if (currentFingerprint) {
      const candidates = (jobDashboard?.jobs ?? []).filter((candidate) => (
        candidate.preview?.jobs.some((job) => job.staging_destination.includes(`/${currentFingerprint}/`))
      ));
      const titleIndexes = (candidate: OrchestrationJob) => new Set(
        candidate.preview?.jobs
          .filter((item) => item.drive_index === driveIndex)
          .map((item) => item.title_index) ?? [],
      );
      const superseded = new Set(candidates
        .filter((candidate) => (
          ['authorized', 'queued', 'paused'].includes(candidate.state)
          && !candidate.executor_attached
        ))
        .filter((candidate) => {
          const pendingTitles = titleIndexes(candidate);
          if (pendingTitles.size === 0) return false;
          return candidates.some((completed) => {
            if (completed.state !== 'completed' || Date.parse(completed.created_at) <= Date.parse(candidate.created_at)) return false;
            const completedTitles = titleIndexes(completed);
            return [...pendingTitles].every((titleIndex) => completedTitles.has(titleIndex));
          });
        })
        .map((candidate) => candidate.job_id));
      const currentCandidates = candidates.filter((candidate) => !superseded.has(candidate.job_id));
      if (bound && currentCandidates.some((candidate) => candidate.job_id === bound.job_id)
        && ['queued', 'authorized', 'paused'].includes(bound.state)) return bound;
      const continuation = ['running', 'pause_requested', 'queued', 'authorized', 'paused']
        .map((state) => currentCandidates
          .filter((candidate) => candidate.state === state)
          .sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at))[0])
        .find(Boolean);
      if (continuation) return continuation;
      if (bound && currentCandidates.some((candidate) => candidate.job_id === bound.job_id)) return bound;
      const review = currentCandidates
        .filter((candidate) => candidate.state === 'awaiting_review')
        .sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at))[0];
      if (review) return review;
      return currentCandidates
        .filter((candidate) => ['completed', 'failed'].includes(candidate.state))
        .sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at))[0];
    }
    if (bound) return bound;
    return undefined;
  }, [driveDashboard?.drives, jobDashboard?.jobs]);

  const keepCompletedDiscInserted = (key: string) => {
    completeDiscAutoEjectAttemptedRef.current.add(key);
    setCompleteDiscAutoEjectDeadlines((current) => Object.fromEntries(
      Object.entries(current).filter(([candidate]) => candidate !== key),
    ));
    setCompleteDiscAutoEjectHolds((current) => ({ ...current, [key]: 'kept' }));
    setReviewNotice('Automatic eject cancelled for this insertion. Use Eject disc when you are ready.');
  };

  useEffect(() => {
    if (!automaticEjectAfterCompletion) {
      setCompleteDiscAutoEjectDeadlines({});
      return;
    }
    const candidates = (driveDashboard?.drives ?? []).flatMap((drive) => {
      if (!drive.has_disc || drive.mapping_status === 'ignored') return [];
      const job = latestJobForDrive(drive.drive_index);
      if (!job || job.state !== 'awaiting_review' || !job.preview) return [];
      const discFingerprint = drive.current_disc_fingerprint
        ?? job.preview.jobs
          .map((item) => item.staging_destination.match(/(?:^|\/)([0-9a-f]{16})(?:\/|$)/)?.[1])
          .find((value): value is string => Boolean(value));
      const expectedTitleIndexes = new Set(job.preview.jobs
        .filter((item) => item.drive_index === drive.drive_index)
        .map((item) => item.title_index));
      const safelyPresentTitleIndexes = new Set([
        ...job.preview.jobs
          .filter((item) => item.drive_index === drive.drive_index && item.prior_library_status === 'present')
          .map((item) => item.title_index),
        ...(pipelineQueue?.items ?? [])
          .filter((item) => (
            discFingerprint
            && item.disc_fingerprint === discFingerprint
            && item.staged_source_available
            && typeof item.title_index === 'number'
          ))
          .map((item) => item.title_index as number),
      ]);
      const skippedTitleIndexes = new Set((pipelineQueue?.title_dispositions ?? [])
        .filter((item) => discFingerprint && item.disc_fingerprint === discFingerprint && item.disposition === 'skip')
        .map((item) => item.title_index));
      const relevantRipComplete = expectedTitleIndexes.size > 0
        && [...expectedTitleIndexes].every((titleIndex) => (
          safelyPresentTitleIndexes.has(titleIndex) || skippedTitleIndexes.has(titleIndex)
        ));
      if (!allKnownTitlesAlreadyInLibrary(job.preview) && !relevantRipComplete) return [];
      const competingWork = (jobDashboard?.jobs ?? []).some((candidate) => (
        candidate.job_id !== job.job_id
        && ['authorized', 'queued', 'running', 'pause_requested'].includes(candidate.state)
        && candidate.preview?.drives.some((item) => item.drive_index === drive.drive_index)
      ));
      return competingWork ? [] : [{ drive, job, key: completeDiscAutoEjectKey(drive, job) }];
    });
    const candidateKeys = new Set(candidates.map((candidate) => candidate.key));
    for (const attempted of completeDiscAutoEjectAttemptedRef.current) {
      if (!candidateKeys.has(attempted)) completeDiscAutoEjectAttemptedRef.current.delete(attempted);
    }
    setCompleteDiscAutoEjectDeadlines((current) => {
      const next: Record<string, number> = {};
      let changed = false;
      for (const candidate of candidates) {
        if (completeDiscAutoEjectHolds[candidate.key] || completeDiscAutoEjectAttemptedRef.current.has(candidate.key)) continue;
        next[candidate.key] = current[candidate.key] ?? Date.now() + 60_000;
        if (current[candidate.key] !== next[candidate.key]) changed = true;
      }
      if (Object.keys(current).some((key) => !(key in next))) changed = true;
      return changed ? next : current;
    });
  }, [automaticEjectAfterCompletion, completeDiscAutoEjectHolds, driveDashboard?.drives, jobDashboard?.jobs, latestJobForDrive, pipelineQueue?.items, pipelineQueue?.title_dispositions]);

  useEffect(() => {
    if (Object.keys(completeDiscAutoEjectDeadlines).length === 0) return;
    const timer = window.setInterval(() => setCompleteDiscAutoEjectClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [completeDiscAutoEjectDeadlines]);

  useEffect(() => {
    const dueKeys = Object.entries(completeDiscAutoEjectDeadlines)
      .filter(([, deadline]) => deadline <= completeDiscAutoEjectClock)
      .map(([key]) => key);
    for (const key of dueKeys) {
      if (completeDiscAutoEjectAttemptedRef.current.has(key)) continue;
      const drive = driveDashboard?.drives.find((candidate) => {
        const job = latestJobForDrive(candidate.drive_index);
        return Boolean(job && completeDiscAutoEjectKey(candidate, job) === key);
      });
      if (!drive) continue;
      completeDiscAutoEjectAttemptedRef.current.add(key);
      void ejectDriveRef.current(drive, true, true).then((ejected) => {
        setCompleteDiscAutoEjectDeadlines((current) => Object.fromEntries(
          Object.entries(current).filter(([candidate]) => candidate !== key),
        ));
        if (!ejected) {
          setCompleteDiscAutoEjectHolds((current) => ({ ...current, [key]: 'failed' }));
        }
      });
    }
  }, [completeDiscAutoEjectClock, completeDiscAutoEjectDeadlines, driveDashboard?.drives, latestJobForDrive]);

  const persistDiscSetup = async (drive: DriveSlot, update: Partial<DiscSetup>) => {
    const key = driveSetupKey(drive);
    const next = { ...getDiscSetup(key), ...update };
    updateDiscSetup(key, update);
    const job = latestJobForDrive(drive.drive_index);
    if (!job || !['awaiting_review', 'authorized', 'queued', 'running'].includes(job.state)) return;
    try {
      const response = await fetch(`/rip/jobs/${job.job_id}/pipeline-settings`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': `pipeline-settings-${job.job_id}-${crypto.randomUUID()}`,
        },
        body: JSON.stringify({
          content_hint: next.contentType || null,
          handbrake_profile_id: next.handbrakeProfile || null,
        }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(typeof payload?.detail === 'string' ? payload.detail : 'Pipeline settings could not be saved.');
      }
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Pipeline settings could not be saved.');
    }
  };

  useEffect(() => {
    const selectedFingerprint = savedJob?.preview?.jobs
      .map((job) => job.staging_destination.match(/(?:^|\/)([0-9a-f]{16})(?:\/|$)/)?.[1])
      .find((value): value is string => Boolean(value));
    const liveDrive = driveDashboard?.drives.find(
      (drive) => drive.current_job_id === savedJob?.job_id,
    ) ?? driveDashboard?.drives.find(
      (drive) => Boolean(selectedFingerprint && drive.current_disc_fingerprint === selectedFingerprint),
    );
    if (!liveDrive?.has_disc) return;
    const latest = latestJobForDrive(liveDrive.drive_index);
    const latestFingerprint = latest?.preview?.jobs
      .map((job) => job.staging_destination.match(/(?:^|\/)([0-9a-f]{16})(?:\/|$)/)?.[1])
      .find((value): value is string => Boolean(value));
    if (
      latest?.preview
      && latest.job_id !== savedJob?.job_id
      && Boolean(selectedFingerprint && latestFingerprint !== selectedFingerprint)
    ) {
      setSavedJob(latest);
      setPreview(latest.preview);
      setReviewNotice('The disc in this drive changed. Showing its newest saved review instead of the earlier disc review.');
      setError('');
    }
  }, [driveDashboard?.drives, latestJobForDrive, savedJob?.job_id, savedJob?.preview?.jobs]);

  const handlePreview = async (event: FormEvent) => {
    event.preventDefault();
    const reportPaths = reportText
      .split(/\r?\n/)
      .map((value) => value.trim())
      .filter(Boolean);
    if (!reportPaths.length || !seriesName.trim()) {
      setError('Add at least one saved preflight report and a canonical series name.');
      return;
    }

    const parsedSeason = season.trim() === '' ? null : Number(season);
    if (parsedSeason !== null && (!Number.isInteger(parsedSeason) || parsedSeason < 0 || parsedSeason > 99)) {
      setError('Season must be between 0 and 99, or blank for Unmatched.');
      return;
    }

    const mediaContexts = Object.fromEntries(
      reportPaths.map((_, index) => [
        `disc-${String(index + 1).padStart(2, '0')}`,
        {
          series_name: seriesName.trim(),
          season: parsedSeason,
          disc_number: index + 1,
        },
      ]),
    );

    setLoading(true);
    setError('');
    setPreview(null);
    setSavedJob(null);
    try {
      const requestPayload: PreviewRequest = {
        report_paths: reportPaths,
        media_contexts: mediaContexts,
        output_root: outputRoot.trim() || null,
      };
      const response = await fetch('/rip/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestPayload),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Preview failed safely.');
      }
      setPreview(payload);
      setPreviewRequest(requestPayload);
      setCreationKey(`create-${crypto.randomUUID()}`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Preview failed safely.');
    } finally {
      setLoading(false);
    }
  };

  const saveDurableJob = async () => {
    if (!previewRequest || !creationKey) return;
    if (!previewRequest.output_root) {
      setError('An existing output root is required before saving a durable execution job.');
      return;
    }
    setSaving(true);
    setError('');
    try {
      const response = await fetch('/rip/jobs', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': creationKey,
        },
        body: JSON.stringify(previewRequest),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Job creation failed safely.');
      }
      setSavedJob(payload);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Job creation failed safely.');
    } finally {
      setSaving(false);
    }
  };

  const selectedDiscFingerprint = preview?.jobs
    .map((job) => job.staging_destination.match(/(?:^|\/)([0-9a-f]{16})(?:\/|$)/)?.[1])
    .find((value): value is string => Boolean(value));
  const soleLoadedDrive = driveDashboard?.drives.filter((drive) => drive.has_disc).length === 1
    ? driveDashboard.drives.find((drive) => drive.has_disc)
    : undefined;
  const selectedDriveSlot = driveDashboard?.drives.find(
    (drive) => drive.current_job_id === savedJob?.job_id,
  ) ?? driveDashboard?.drives.find(
    (drive) => drive.current_disc_fingerprint === selectedDiscFingerprint,
  ) ?? soleLoadedDrive
    ?? driveDashboard?.drives.find(
      (drive) => drive.drive_index === preview?.drives[0]?.drive_index,
    );
  const catalogueSupportRequired = Boolean(
    preview?.drives.some((drive) => drive.metadata_status === 'support-required'),
  );
  const supportPolicy = catalogueSupportStatus?.policy;
  const supportLookupCredits = Math.floor(
    supportAmountCents / Math.max(1, supportRateCents),
  );
  const scopedPipelineItems = pipelineQueue?.items.filter((item) =>
    attentionOnly
      ? ['failed', 'review_required'].includes(item.state)
      : queueOnly || Boolean(selectedDiscFingerprint && item.disc_fingerprint === selectedDiscFingerprint)
  ) ?? [];
  const latestPipelineItems = Array.from(scopedPipelineItems.reduce((items, item) => {
    const key = item.disc_fingerprint && typeof item.title_index === 'number'
      ? `${item.disc_fingerprint}:${item.title_index}`
      : item.media_id;
    const previous = items.get(key);
    items.set(key, preferredPipelineItem(previous, item));
    return items;
  }, new Map<string, PipelineQueueItem>()).values());
  const activePipelineItems = latestPipelineItems.filter((item) => !['completed', 'discarded'].includes(item.state));
  const likelyRemovableCount = activePipelineItems.filter((item) => item.likely_removable).length;
  const visiblePipelineItems = activePipelineItems
    .filter((item) => !showLikelyRemovableOnly || item.likely_removable)
    .sort((left, right) => Number(right.likely_removable) - Number(left.likely_removable));
  const sequenceRecoveryByDisc = new Map<string, { firstMediaId: string; itemCount: number }>();
  const seriesResolutionRecoveryByDisc = new Map<string, string>();
  for (const item of visiblePipelineItems) {
    if (item.review_code === 'gemini_series_resolution_uncertain') {
      const recoveryKey = item.disc_fingerprint || item.media_id;
      if (!seriesResolutionRecoveryByDisc.has(recoveryKey)) {
        seriesResolutionRecoveryByDisc.set(recoveryKey, item.media_id);
      }
    }
    if (!item.disc_fingerprint || !requiresDiscWideTvRecovery(item)) continue;
    const recovery = sequenceRecoveryByDisc.get(item.disc_fingerprint);
    if (recovery) recovery.itemCount += 1;
    else sequenceRecoveryByDisc.set(item.disc_fingerprint, { firstMediaId: item.media_id, itemCount: 1 });
  }
  const queuedTranscodeItems = visiblePipelineItems.filter((item) => item.stage === 'transcode' && item.state === 'queued');
  const runningTranscodeItems = visiblePipelineItems.filter((item) => item.stage === 'transcode' && item.state === 'running');
  const queuedOrganizationItems = visiblePipelineItems.filter((item) => item.stage === 'organize' && item.state === 'queued');
  const runningOrganizationItems = visiblePipelineItems.filter((item) => item.stage === 'organize' && item.state === 'running');
  const visibleRipJobs = (jobDashboard?.jobs ?? []).filter((job) => {
    if (!['queued', 'running', 'pause_requested'].includes(job.state)) return false;
    if (queueOnly) return true;
    return Boolean(savedJob?.job_id && job.job_id === savedJob.job_id);
  });
  const existingRipsNeedAnalysis = visiblePipelineItems.some((item) =>
    item.stage === 'identify'
    && item.state === 'review_required'
    && requiresDiscWideTvRecovery(item)
  );
  const existingRipsInPipeline = existingRipsRestarted || visiblePipelineItems.some(
    (item) => item.disc_fingerprint === selectedDiscFingerprint && item.media_id.includes('-recovery-'),
  );
  const currentTitleOutcome = (titleIndex: number) => latestPipelineItems.find(
    (item) => item.title_index === titleIndex,
  );
  const heldLibraryTitles = preview?.held_titles ?? [];
  const heldLibraryTitleIndexes = new Set(heldLibraryTitles.map((item) => item.title_index));
  const separatedHeldLibraryTitles = heldLibraryTitles.filter(
    (item) => !preview?.jobs.some((job) => job.title_index === item.title_index),
  );
  const missingLibraryTitleCount = preview?.jobs.filter(
    (job) => ['clear', 'not-checked'].includes(job.collision_status),
  ).length ?? 0;
  const allReviewedTitlesAlreadyInLibrary = allKnownTitlesAlreadyInLibrary(preview);
  const selectedDriveLabel = selectedDriveSlot?.disc_label || '';
  const selectedJobId = savedJob?.job_id;
  const selectedJobState = savedJob?.state;
  useEffect(() => {
    if (!selectedJobId || selectedJobState !== 'awaiting_review') {
      setRecoverableExistingTitleCount(0);
      setRecoverableExistingTitleIndexes([]);
      return;
    }
    let cancelled = false;
    fetch(`/rip/jobs/${selectedJobId}/existing-rips/preview`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm_search: true }),
    })
      .then(async (response) => response.ok ? response.json() as Promise<ExistingRipRecoveryPlan> : null)
      .then((recovery) => {
        if (cancelled) return;
        const titleIndexes = new Set(recovery?.candidates.map((candidate) => candidate.title_index) ?? []);
        setRecoverableExistingTitleCount(titleIndexes.size);
        setRecoverableExistingTitleIndexes([...titleIndexes].sort((left, right) => left - right));
      })
      .catch(() => {
        if (!cancelled) {
          setRecoverableExistingTitleCount(0);
          setRecoverableExistingTitleIndexes([]);
        }
      });
    return () => { cancelled = true; };
  }, [selectedJobId, selectedJobState]);
  useEffect(() => {
    if (!selectedJobId || selectedJobState !== 'queued') {
      setFailedCleanupPlan(null);
      return;
    }
    let cancelled = false;
    fetch(`/rip/jobs/${selectedJobId}/failed-attempts`)
      .then((response) => response.ok ? response.json() : null)
      .then((payload) => { if (!cancelled) setFailedCleanupPlan(payload); })
      .catch(() => { if (!cancelled) setFailedCleanupPlan(null); });
    return () => { cancelled = true; };
  }, [selectedJobId, selectedJobState]);
  const suggestedUnmatchedSeries = inferCanonicalSeriesFromDiscLabel(selectedDriveLabel);
  const suggestedSeason = inferDiscSeason(selectedDriveLabel);
  useEffect(() => {
    setUnmatchedEpisodeStart('');
    setUnmatchedEpisodeEnd('');
  }, [selectedDiscFingerprint]);
  const reviewedCoverageSeries = unmatchedSeriesName.trim() || suggestedUnmatchedSeries;
  useEffect(() => {
    if (!reviewedCoverageSeries) {
      setLearnedCoverage(null);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      const response = await fetch(`/rip/pipeline/learned-coverage?series_name=${encodeURIComponent(reviewedCoverageSeries)}`);
      if (!response.ok || cancelled) return;
      const payload = await response.json() as { coverage: LearnedSeriesCoverage };
      if (!cancelled) setLearnedCoverage(payload.coverage);
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [reviewedCoverageSeries]);
  const visibleMatchingPerformance = matchingPerformance
    .filter((run) => queueOnly || !selectedDiscFingerprint || run.disc_fingerprint === selectedDiscFingerprint)
    .slice(0, 5);
  const runAllSeasonAnalysis = async () => {
    if (!selectedDiscFingerprint) return;
    const reviewedSeries = unmatchedSeriesName.trim() || suggestedUnmatchedSeries;
    if (!reviewedSeries) {
      setReviewNotice('Enter the canonical TV series name before starting all-season analysis.');
      return;
    }
    const rawEpisodeStart = unmatchedEpisodeStart.trim();
    const rawEpisodeEnd = unmatchedEpisodeEnd.trim();
    if (Boolean(rawEpisodeStart) !== Boolean(rawEpisodeEnd)) {
      setReviewNotice('Enter both the first and last episode from the disc paperwork, or leave both blank.');
      return;
    }
    let episodeStart: number | null = null;
    let episodeEnd: number | null = null;
    if (rawEpisodeStart && rawEpisodeEnd) {
      if (suggestedSeason === null || !/^\d{1,3}$/.test(rawEpisodeStart) || !/^\d{1,3}$/.test(rawEpisodeEnd)) {
        setReviewNotice('A paperwork episode range requires a known season and valid positive episode numbers.');
        return;
      }
      episodeStart = Number(rawEpisodeStart);
      episodeEnd = Number(rawEpisodeEnd);
      if (episodeStart < 1 || episodeEnd < episodeStart || episodeEnd - episodeStart > 49) {
        setReviewNotice('The paperwork episode range is invalid or too broad.');
        return;
      }
    }
    const scopeLabel = episodeStart !== null && episodeEnd !== null && suggestedSeason !== null
      ? `against reviewed candidates S${String(suggestedSeason).padStart(2, '0')}E${String(episodeStart).padStart(2, '0')}-E${String(episodeEnd).padStart(2, '0')}`
      : suggestedSeason === null ? 'across every aired season' : `against Season ${suggestedSeason}`;
    if (!window.confirm(`Analyze the held MKVs as “${reviewedSeries}” ${scopeLabel}? This reads short audio samples, transcribes them locally, and queries TMDb episode metadata.${discGeminiFallback ? ' If local matching remains ambiguous, the configured Gemini fallback may receive bounded transcript excerpts and candidate episode metadata.' : ' Gemini fallback is disabled for this scan.'} It does not rename, move, delete, or transcode media.`)) return;
    setControlling(true);
    try {
      const response = await fetch('/rip/pipeline/analyze-unmatched-disc', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          disc_fingerprint: selectedDiscFingerprint,
          series_name: reviewedSeries,
          season: suggestedSeason,
          episode_start: episodeStart,
          episode_end: episodeEnd,
          confirm_media_read: true,
          confirm_provider_lookup: true,
          confirm_external_fallback: discGeminiFallback,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'All-season analysis could not be started.');
      setReviewNotice(`Started ${scopeLabel} evidence analysis for ${payload.item_count} title(s).`);
      const queueResponse = await fetch('/rip/pipeline/items');
      if (queueResponse.ok) setPipelineQueue(await queueResponse.json());
    } catch (requestError) {
      setReviewNotice(requestError instanceof Error ? requestError.message : 'All-season analysis could not be started.');
    } finally {
      setControlling(false);
    }
  };
  const analyzeHeldItemAsTv = async (item: PipelineQueueItem, seriesOverride?: string) => {
    const fingerprint = item.disc_fingerprint;
    const recoveredDiscLabel = item.media_id.split('--disc-', 1)[0];
    const recoveredSeries = inferCanonicalSeriesFromDiscLabel(recoveredDiscLabel);
    const recoveredSeason = inferDiscSeason(recoveredDiscLabel);
    const dashboardSeries = selectedDiscFingerprint === fingerprint ? unmatchedSeriesName.trim() : '';
    const reviewedSeries = seriesOverride?.trim() || dashboardSeries || recoveredSeries;
    if (!fingerprint || !reviewedSeries) {
      setReviewNotice('Open this disc on the Disc Dashboard and enter its canonical TV series name.');
      return;
    }
    const scopeLabel = recoveredSeason === null ? 'across all aired seasons' : `within Season ${recoveredSeason}`;
    if (!window.confirm(`Retry every held title from this disc as TV episodes of “${reviewedSeries}” ${scopeLabel}? RipWeaver will reuse valid saved evidence or read short staged-MKV samples, then query episode providers. It will not rerip, rename, move, delete, or transcode media.`)) return;
    setSubmittingSeriesRecovery(fingerprint);
    setError('');
    try {
      const response = await fetch('/rip/pipeline/analyze-unmatched-disc', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          disc_fingerprint: fingerprint,
          series_name: reviewedSeries,
          season: recoveredSeason,
          confirm_media_read: true,
          confirm_provider_lookup: true,
          confirm_external_fallback: automaticGeminiFallback,
        }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'TV episode analysis could not be started.');
      setReviewNotice(`Started ${recoveredSeason === null ? 'TV episode' : `Season ${recoveredSeason}`} analysis for ${payload.item_count} held title(s) as “${reviewedSeries}”.`);
      const queueResponse = await fetch('/rip/pipeline/items');
      if (queueResponse.ok) setPipelineQueue(await queueResponse.json());
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'TV episode analysis could not be started.';
      setError(message);
      setReviewNotice(message);
    } finally {
      setSubmittingSeriesRecovery(null);
    }
  };
  const analyzeWithSceneDescription = async (item: PipelineQueueItem) => {
    const description = sceneDescriptionDrafts[item.media_id]?.trim() || '';
    const fingerprint = item.disc_fingerprint;
    const recoveredDiscLabel = item.media_id.split('--disc-', 1)[0];
    const recoveredSeason = inferDiscSeason(recoveredDiscLabel);
    const evidenceSeries = item.identification_attempts
      ?.map((attempt) => attempt.summary.candidate_series_name)
      .find((value): value is string => typeof value === 'string' && Boolean(value.trim()));
    const dashboardSeries = selectedDiscFingerprint === fingerprint ? unmatchedSeriesName.trim() : '';
    const reviewedSeries = evidenceSeries?.trim() || dashboardSeries || inferCanonicalSeriesFromDiscLabel(recoveredDiscLabel);
    if (!fingerprint || !reviewedSeries) {
      setError('The canonical TV series is unavailable. Open this disc on the Disc Dashboard and enter the series name first.');
      return;
    }
    if (description.length < 3) {
      setError('Describe at least one scene before asking Gemini to review the episode again.');
      return;
    }
    if (!window.confirm(`Send your scene description for this exact staged title to Gemini with bounded local evidence and the allowed ${reviewedSeries} episode candidates? RipWeaver will retry every still-held title from this disc so the remaining episode set can be considered. The MKV, local paths, credentials, and full transcripts are not transmitted.`)) return;
    setSubmittingSceneReviewId(item.media_id);
    setError('');
    setGeminiProgress((current) => ({ ...current, [item.media_id]: 'Submitting your scene-guided episode review...' }));
    try {
      const response = await fetch('/rip/pipeline/analyze-unmatched-disc', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          disc_fingerprint: fingerprint,
          series_name: reviewedSeries,
          season: recoveredSeason,
          confirm_media_read: true,
          confirm_provider_lookup: true,
          confirm_external_fallback: true,
          reviewer_scene_descriptions: { [item.media_id]: description },
        }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'The scene-guided Gemini review could not be started.');
      setGeminiProgress((current) => ({ ...current, [item.media_id]: 'Using your scene description with the remaining disc episodes...' }));
      setReviewNotice(`Started a scene-guided episode review for ${payload.item_count} held title(s) from this disc.`);
      const queueResponse = await fetch('/rip/pipeline/items');
      if (queueResponse.ok) setPipelineQueue(await queueResponse.json());
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'The scene-guided Gemini review could not be started.';
      setError(message);
      setGeminiProgress((current) => ({ ...current, [item.media_id]: `Scene-guided review did not start: ${message}` }));
    } finally {
      setSubmittingSceneReviewId(null);
    }
  };
  const classifyAsMovieExtras = async () => {
    if (!selectedDiscFingerprint) return;
    if (!window.confirm(`Analyze these held MKVs as movie or TV-movie bonus features? This reads bounded local evidence.${automaticGeminiFallback ? ' Automatic Gemini fallback is enabled, so selected short evidence and allowed candidate names may be sent to Gemini if local matching remains ambiguous.' : ' Gemini fallback is disabled, so ambiguous titles will remain available for review.'} It does not rip, rename, move, delete, or transcode media.`)) return;
    setControlling(true);
    try {
      const response = await fetch('/rip/pipeline/classify-unmatched-disc', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          disc_fingerprint: selectedDiscFingerprint,
          classification: 'extras',
          confirm_classification: true,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Movie bonus-feature analysis could not be started.');
      const mediaIds = Array.isArray(payload.media_ids) ? payload.media_ids.filter((value: unknown): value is string => typeof value === 'string') : [];
      setReviewNotice(`Routed ${payload.queued_item_count} title(s) to movie bonus-feature identification${automaticGeminiFallback ? '; waiting for local evidence before Gemini fallback' : ''}.`);
      if (automaticGeminiFallback && mediaIds.length > 0) {
        const deadline = Date.now() + 30000;
        let ready = false;
        while (Date.now() < deadline) {
          const statusResponse = await fetch('/rip/pipeline/items');
          if (statusResponse.ok) {
            const statusPayload = await statusResponse.json();
            setPipelineQueue(statusPayload);
            const selected = (statusPayload.items || []).filter((item: PipelineQueueItem) => mediaIds.includes(item.media_id));
            ready = selected.length === mediaIds.length && selected.every((item: PipelineQueueItem) => item.state === 'review_required' && item.review_code === 'gemini_evidence_required');
            if (ready) break;
          }
          await new Promise((resolve) => window.setTimeout(resolve, 500));
        }
        if (!ready) throw new Error('Bonus-feature routing completed, but local evidence did not become ready for Gemini within 30 seconds. Use Retry local evidence and Gemini on the held items.');
        const executionResponse = await fetch('/rip/pipeline/gemini/execute', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ media_ids: mediaIds, confirm_media_read: true, confirm_external_transmission: true }),
        });
        const execution = await responsePayload(executionResponse);
        if (!executionResponse.ok) throw new Error(typeof execution.detail === 'string' ? execution.detail : 'Gemini evidence processing could not be queued.');
        setReviewNotice(`Collecting local evidence and running Gemini fallback for ${execution.item_count} movie bonus title(s).`);
      }
      const queueResponse = await fetch('/rip/pipeline/items');
      if (queueResponse.ok) setPipelineQueue(await queueResponse.json());
    } catch (requestError) {
      setReviewNotice(requestError instanceof Error ? requestError.message : 'Movie bonus-feature analysis could not be started.');
    } finally {
      setControlling(false);
    }
  };
  const runHeldGeminiBatch = async () => {
    const mediaIds = visiblePipelineItems
      .filter((item) => item.state === 'review_required' && ['gemini_evidence_required', 'gemini_catalog_unavailable'].includes(item.review_code || ''))
      .map((item) => item.media_id);
    if (mediaIds.length === 0) return;
    if (!window.confirm(`Collect bounded local evidence for ${mediaIds.length} held bonus title(s), then send only selected short evidence and allowed candidate names to Gemini? No MKV, local path, credential, or full transcript is transmitted.`)) return;
    setControlling(true);
    try {
      const response = await fetch('/rip/pipeline/gemini/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ media_ids: mediaIds, confirm_media_read: true, confirm_external_transmission: true }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Gemini evidence processing could not be queued.');
      setReviewNotice(`Collecting local evidence and running Gemini fallback for ${payload.item_count} bonus title(s).`);
      const queueResponse = await fetch('/rip/pipeline/items');
      if (queueResponse.ok) setPipelineQueue(await queueResponse.json());
    } catch (requestError) {
      setReviewNotice(requestError instanceof Error ? requestError.message : 'Gemini evidence processing could not be queued.');
    } finally {
      setControlling(false);
    }
  };

  const mappingDevices = driveDashboard?.drives.filter((drive): drive is DriveSlot & { mapping_id: string } => Boolean(drive.mapping_id)) ?? [];
  const mappingSummary = driveDashboard?.mapping_summary ?? {
    trusted: mappingDevices.filter((drive) => drive.mapping_status === 'trusted').length,
    ignored: mappingDevices.filter((drive) => drive.mapping_status === 'ignored').length,
    unmapped: mappingDevices.filter((drive) => drive.mapping_status === 'unmapped').length,
  };
  const mappingNeedsReview = mappingDevices.some((drive) => drive.mapping_status === 'unmapped');
  const identityUnavailableDrives = mappingDevices.filter((drive) => drive.mapping_warning === 'identity_unavailable');
  const mappedLoadedDiscWillContinue = Boolean(
    continueAfterMapping
    && jobDashboard?.automatic_processing_enabled
    && mappingDevices.some((drive) => (
      drive.has_disc
      && drive.makemkv_confirmed !== false
      && mappingDraft[drive.mapping_id] === 'trusted'
    )),
  );

  return (
    <div className="h-full overflow-auto space-y-6">
      <div className={queueOnly || attentionOnly ? 'hidden' : 'contents'}>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold heading-gradient mb-1">Disc Dashboard</h2>
          <p className="text-sm text-[var(--text-muted)]">See every detected optical drive, configure each inserted disc, and follow it through the pipeline.</p>
        </div>
        {pipelineQueue && (
          <div className="flex flex-wrap items-center justify-end gap-2 rounded-xl border border-[var(--border-color)] bg-black/15 p-3">
            <span className={`mr-1 text-sm font-semibold ${pipelineQueue.paused ? 'text-amber-200' : 'text-green-200'}`}>
              {pipelineQueue.paused && pipelineQueue.startup_resume_in_seconds
                ? `Queue resumes in ${pipelineQueue.startup_resume_in_seconds}s`
                : `Queue ${pipelineQueue.paused ? 'paused' : 'active'}`}
            </span>
            {pipelineQueue.paused ? (
              <button type="button" className="btn btn-primary" disabled={controlling} onClick={() => controlPipeline('resume')}>Resume processing</button>
            ) : (
              <button type="button" className="btn btn-secondary" disabled={controlling} onClick={() => controlPipeline('pause')} title="Let active work settle, then prevent RipWeaver from starting another disc or downstream item.">Pause after active work</button>
            )}
          </div>
        )}
      </div>

      {showDriveMappingWizard && mappingDevices.length > 0 && driveDashboard?.mapping_plan_sha256 && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/75 p-4" role="dialog" aria-modal="true" aria-labelledby="drive-mapping-wizard-title">
          <div className="w-full max-w-4xl max-h-[90vh] overflow-y-auto rounded-2xl border border-blue-300/30 bg-[var(--bg-secondary)] p-6 shadow-2xl space-y-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="text-xs font-semibold uppercase tracking-wider text-blue-300">Optical drive setup</div>
                <h3 id="drive-mapping-wizard-title" className="mt-1 text-2xl font-bold text-white">Choose the drives RipWeaver may use</h3>
                <p className="mt-2 max-w-3xl text-sm text-[var(--text-muted)]">
                  MakeMKV slot numbers can move after a reboot or USB reconnect. RipWeaver follows a hashed Windows hardware identity instead. A new or changed identity is blocked until you save this review.
                </p>
              </div>
              <button type="button" className="btn btn-secondary" disabled={savingDriveMappings} onClick={() => setShowDriveMappingWizard(false)}>
                Finish later
              </button>
            </div>

            {mappingDevices.some((drive) => drive.mapping_warning === 'possible_identity_change') && (
              <div className="rounded-xl border border-amber-400/40 bg-amber-500/15 p-4 text-sm text-amber-100">
                A USB or Windows hardware identity appears to have changed. RipWeaver did not reuse the old approval. Confirm the physical devices below; absent previously trusted identities will be retired when you save.
              </div>
            )}

            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="text-sm text-blue-100">{mappingDevices.length} optical {mappingDevices.length === 1 ? 'device' : 'devices'} detected</div>
              <button
                type="button"
                className="btn btn-primary text-sm"
                disabled={savingDriveMappings}
                onClick={() => setMappingDraft(Object.fromEntries(mappingDevices.map((drive) => [drive.mapping_id, 'trusted'])))}
              >
                Use all {mappingDevices.length} detected drives
              </button>
            </div>

            <div className="space-y-3">
              {mappingDevices.map((drive) => {
                const choice = mappingDraft[drive.mapping_id] ?? (drive.mapping_status === 'ignored' ? 'ignored' : 'trusted');
                return (
                  <div key={`wizard-${drive.mapping_id}`} className={`rounded-xl border p-4 ${drive.mapping_warning === 'possible_identity_change' ? 'border-amber-400/40 bg-amber-500/10' : 'border-[var(--border-color)] bg-black/15'}`}>
                    <div className="flex flex-wrap items-start justify-between gap-4">
                      <div className="min-w-0">
                        <div className="font-semibold text-white">{drive.display_name || 'Optical device'}</div>
                        <div className="mt-1 text-xs text-[var(--text-muted)]">
                          {drive.connection_type ? `${drive.connection_type.toUpperCase()} · ` : ''}{drive.makemkv_confirmed === false ? 'provisional RipWeaver slot' : 'current MakeMKV slot'} {drive.drive_index + 1}
                        </div>
                        {drive.mapping_warning === 'possible_identity_change' && (
                          <div className="mt-2 text-xs text-amber-200">
                            Possible reconnected device: this safe descriptor resembles {drive.prior_similar_mapping_count || 1} previously trusted {drive.prior_similar_mapping_count === 1 ? 'identity' : 'identities'}.
                          </div>
                        )}
                        {drive.makemkv_confirmed === false && (
                          <div className="mt-2 text-xs text-amber-200">
                            Windows sees this device, but MakeMKV did not confirm its current slot. You may map it now, but disc work stays blocked until a later read-only refresh confirms it.
                          </div>
                        )}
                      </div>
                      <div className="flex rounded-lg border border-[var(--border-color)] p-1">
                        <button
                          type="button"
                          className={`rounded-md px-4 py-2 text-sm ${choice === 'trusted' ? 'bg-green-500/20 text-green-200' : 'text-[var(--text-muted)] hover:text-white'}`}
                          onClick={() => setMappingDraft((current) => ({ ...current, [drive.mapping_id]: 'trusted' }))}
                        >
                          Use
                        </button>
                        <button
                          type="button"
                          className={`rounded-md px-4 py-2 text-sm ${choice === 'ignored' ? 'bg-slate-500/30 text-white' : 'text-[var(--text-muted)] hover:text-white'}`}
                          onClick={() => setMappingDraft((current) => ({ ...current, [drive.mapping_id]: 'ignored' }))}
                        >
                          Ignore
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>

            {jobDashboard?.automatic_processing_enabled && mappingDevices.some((drive) => drive.has_disc && drive.makemkv_confirmed !== false) && (
              <label className="flex items-start gap-3 rounded-xl border border-blue-400/25 bg-blue-500/10 p-4 text-sm text-blue-100">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={continueAfterMapping}
                  onChange={(event) => setContinueAfterMapping(event.target.checked)}
                />
                <span>
                  <span className="font-semibold">Continue automatic processing for loaded discs after saving.</span>
                  <span className="mt-1 block text-xs text-blue-100/75">This may immediately read disc inventories and start already-authorized automatic ripping on drives marked Use. Clear this box to save the map only.</span>
                </span>
              </label>
            )}

            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--border-color)] pt-4">
              <div className="text-xs text-[var(--text-muted)]">
                Only hashed identities and sanitized model/connection descriptions are saved. Raw Windows USB IDs are not stored in this map.
              </div>
              <div className="flex gap-2">
                <button type="button" className="btn btn-secondary" disabled={savingDriveMappings} onClick={() => setShowDriveMappingWizard(false)}>Cancel</button>
                <button type="button" className="btn btn-primary" disabled={savingDriveMappings} onClick={() => void saveDriveMappingPlan()}>
                  {savingDriveMappings ? 'Saving drive setup…' : mappedLoadedDiscWillContinue ? 'Save and continue loaded discs' : 'Save drive setup'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {jobDashboard && (
        <div className="glass-panel rounded-xl p-5 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="font-bold text-white">Optical drives</div>
              <div className="text-sm text-[var(--text-muted)]">
                {jobDashboard.automatic_processing_enabled ? 'Automatic processing requested' : 'Automatic processing disabled'} · {driveDashboard?.automatic_discovery_paused ? 'automatic drive discovery paused' : driveDashboard?.status === 'ready' ? 'drive status refreshed' : 'waiting for read-only refresh'}
              </div>
            </div>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => void refreshDrives()}
              disabled={refreshingDrives || driveDashboard?.refresh_in_progress || driveDashboard?.refresh_deferred || Boolean(driveDashboard?.busy_drive_indexes?.length)}
              title={driveDashboard?.busy_drive_indexes?.length ? 'All-drive MakeMKV discovery waits until active optical work finishes.' : undefined}
            >
              {driveDashboard?.refresh_deferred
                ? 'Drive refresh queued safely'
                : refreshingDrives || driveDashboard?.refresh_in_progress
                  ? 'Reading drive slots (up to 2 minutes)…'
                  : driveDashboard?.automatic_discovery_paused
                    ? 'Retry drive refresh (read-only)'
                    : driveDashboard?.busy_drive_indexes?.length
                      ? 'Refresh unavailable while drives are active'
                      : 'Refresh drives (read-only)'}
            </button>
          </div>
          {driveDashboard?.automatic_discovery_paused && (
            <div className="rounded-lg border border-red-400/30 bg-red-500/10 p-3 text-sm text-red-100">
              <div className="font-semibold">Automatic drive discovery paused after repeated MakeMKV timeouts</div>
              <div className="mt-1 text-xs text-red-100/85">RipWeaver will not start another all-drive MakeMKV scan until Windows reports a physical drive or media change, or you explicitly retry the read-only refresh. Lightweight Windows drive checks remain active.</div>
            </div>
          )}
          {driveDashboard?.refresh_deferred && (
            <div className="rounded-lg border border-blue-400/30 bg-blue-500/10 p-3 text-sm text-blue-100">Full MakeMKV drive discovery is deferred until all active optical work finishes. No additional MakeMKV CLI was started; the cached dashboard remains available meanwhile.</div>
          )}
          {driveDashboard?.status !== 'ready' && (
            <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-100">
              <div>
                {driveDashboard?.refresh_in_progress
                  ? driveDashboard.error_code === 'timeout'
                    ? 'A new read-only MakeMKV discovery is running now. The previous attempt timed out; its recovery message will be replaced when this retry finishes.'
                    : 'Read-only MakeMKV drive discovery is running now. Keep the optical drives connected while RipWeaver waits for their slot information.'
                  : driveDashboard?.automatic_discovery_paused
                    ? `MakeMKV timed out ${driveDashboard.automatic_discovery_timeout_count ?? 2} consecutive times. Automatic retries are suspended so RipWeaver does not repeatedly probe the optical drives.`
                  : driveDashboard?.status === 'error'
                  ? driveDashboard.error_code === 'timeout'
                    ? 'MakeMKV starts, but it could not enumerate the optical drives. This usually means one drive, USB/SATA enclosure, or Windows optical-device query is not responding.'
                    : 'Drive discovery did not complete. Wait for active MakeMKV work to finish, close any separate MakeMKV window, and verify the MakeMKVCLI path in Settings.'
                  : 'Select “Refresh drives” to perform one read-only MakeMKV slot discovery. It enumerates loaded and empty trays but does not inventory titles or start ripping.'}
              </div>
              {!driveDashboard?.refresh_in_progress && driveDashboard?.error_code === 'timeout' && (
                <ol className="mt-3 list-decimal space-y-1 pl-5 text-xs text-amber-100/85">
                  <li>Close the MakeMKV desktop application and wait for any current rip to stop.</li>
                  <li>Power-cycle external optical drives or their USB hub, then reconnect drives one at a time.</li>
                  <li>Confirm each drive appears normally in Windows before reconnecting the next one.</li>
                  <li>Retry the two-minute read-only refresh. If it fails again, restart Windows to reset the optical driver stack.</li>
                </ol>
              )}
              {!driveDashboard?.refresh_in_progress && driveDashboard?.status === 'error' && (
                <div className="mt-3 flex flex-wrap gap-2">
                  <button type="button" className="btn btn-secondary text-xs" onClick={() => void refreshDrives(120)} disabled={refreshingDrives || driveDashboard?.refresh_in_progress || driveDashboard?.refresh_deferred || Boolean(driveDashboard?.busy_drive_indexes?.length)}>
                    {driveDashboard.error_code === 'timeout' ? 'Retry after checking drives' : 'Retry drive refresh'}
                  </button>
                  {driveDashboard.error_code !== 'timeout' && (
                    <button type="button" className="btn btn-secondary text-xs" onClick={onOpenSettings}>
                      Repair MakeMKVCLI path
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
          {identityUnavailableDrives.length > 0 && (
            <div className="rounded-xl border border-amber-400/40 bg-amber-500/10 p-4 text-sm text-amber-100">
              <div className="font-semibold">Detected optical drives are waiting for Windows identity confirmation</div>
              <div className="mt-1 text-xs text-amber-100/80">
                {identityUnavailableDrives.length} {identityUnavailableDrives.length === 1 ? 'drive is' : 'drives are'} shown below but locked because the bounded Windows hardware query did not finish. One read-only refresh can retry identity confirmation; RipWeaver will not inventory or rip an unapproved drive.
              </div>
            </div>
          )}
          {mappingDevices.length > 0 && (
            <div className={`rounded-xl border p-4 ${mappingNeedsReview ? 'border-amber-400/40 bg-amber-500/10' : 'border-blue-400/25 bg-blue-500/10'}`}>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <div className={mappingNeedsReview ? 'font-semibold text-amber-100' : 'font-semibold text-blue-100'}>
                    {mappingNeedsReview ? 'Optical drive setup needs review' : 'Optical drive mapping is ready'}
                  </div>
                  <div className="mt-1 text-xs text-[var(--text-muted)]">
                    {mappingSummary.trusted} trusted · {mappingSummary.ignored} ignored · {mappingSummary.unmapped} awaiting review. MakeMKV slot changes are resolved through hashed Windows identities.
                  </div>
                </div>
                <button type="button" className={mappingNeedsReview ? 'btn btn-primary' : 'btn btn-secondary'} onClick={() => setShowDriveMappingWizard(true)}>
                  {mappingNeedsReview ? 'Set up drives' : 'Manage drive mapping'}
                </button>
              </div>
            </div>
          )}
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
            {!driveDashboard || driveDashboard.drives.length === 0 ? (
              <div className="rounded-xl border border-dashed border-[var(--border-color)] p-8 text-center">
                <div className="text-5xl opacity-40 mb-3">▱</div>
                <div className="font-semibold text-white">No optical drives detected</div>
                <div className="text-sm text-[var(--text-muted)] mt-1">Use one read-only refresh to check Windows and MakeMKV drive slots.</div>
              </div>
            ) : driveDashboard.drives.map((drive) => {
              const driveKey = driveSetupKey(drive);
              const setup = getDiscSetup(driveKey);
              const driveTrusted = !drive.mapping_status || drive.mapping_status === 'trusted';
              const mediaCheckPending = drive.makemkv_confirmed === false && !drive.has_disc;
              if (!driveTrusted) {
                const identityUnavailable = drive.mapping_warning === 'identity_unavailable';
                const ignored = drive.mapping_status === 'ignored';
                const lockedStatus = ignored
                  ? 'ignored'
                  : identityUnavailable
                    ? 'identity unavailable'
                    : 'approval required';
                return (
                  <div key={driveKey} className={`rounded-xl border p-4 space-y-4 ${ignored ? 'border-slate-500/40 bg-slate-500/10' : 'border-amber-400/50 bg-amber-500/10'}`}>
                    <div className="flex items-center justify-between gap-3">
                      <span className={`text-4xl ${drive.has_disc || mediaCheckPending ? 'text-amber-300' : 'text-slate-600'}`} aria-label={drive.has_disc ? 'Disc inserted' : mediaCheckPending ? 'Media check pending' : 'Empty tray'}>{drive.has_disc ? '●' : '▱'}</span>
                      <div className="min-w-0 flex-1">
                        <div className="font-semibold text-white">Optical drive {drive.drive_index + 1}{drive.display_name ? ` · ${drive.display_name}` : ''}{drive.disc_label ? ` — ${drive.disc_label}` : ''}</div>
                        <div className={`text-xs font-bold uppercase ${ignored ? 'text-slate-300' : 'text-amber-200'}`}>
                          {drive.has_disc ? `disc inserted · ${lockedStatus}` : mediaCheckPending ? `media check pending · ${lockedStatus}` : `empty tray · ${lockedStatus}`}
                        </div>
                      </div>
                    </div>
                    <div className={`rounded-lg border p-3 text-sm ${ignored ? 'border-slate-400/25 bg-slate-500/10 text-slate-200' : 'border-amber-400/30 bg-amber-500/10 text-amber-100'}`}>
                      {ignored
                        ? 'This optical device is ignored. Manage drive mapping to approve it before RipWeaver can use it.'
                        : identityUnavailable
                          ? 'MakeMKV detected this slot, but Windows did not return its hardware identity in time. The drive remains visible and safely locked; a read-only refresh retries the identity lookup.'
                          : 'This device was detected but has not been approved. Open drive setup and choose Use before RipWeaver can read or rip it.'}
                    </div>
                    {drive.mapping_id && (
                      <button type="button" className="btn btn-secondary w-full" onClick={() => setShowDriveMappingWizard(true)}>
                        {ignored ? 'Manage drive mapping' : 'Set up this drive'}
                      </button>
                    )}
                  </div>
                );
              }
              const job = latestJobForDrive(drive.drive_index);
              const driveBusy = driveDashboard.busy_drive_indexes?.includes(drive.drive_index) ?? false;
              const physicalDriveOperation = driveDashboard.physical_drive_operations?.[drive.drive_index];
              const driveRipping = physicalDriveOperation === 'MakeMKV rip' || (job?.state === 'running' && job.executor_attached);
              const drivePreparing = driveBusy && !driveRipping;
              const discIdentityNeedsVerification = drive.has_disc
                && !drive.current_disc_fingerprint
                && !driveRipping
                && !drivePreparing;
              const driveJobs = (jobDashboard?.jobs ?? []).filter((candidate) => candidate.preview?.drives.some((item) => item.drive_index === drive.drive_index));
              const currentDiscJobs = drive.current_disc_fingerprint
                ? driveJobs.filter((candidate) => previewHasDiscFingerprint(candidate.preview, drive.current_disc_fingerprint))
                : [];
              const failedRipJob = currentDiscJobs.find((candidate) => (
                candidate.state === 'failed'
                && (candidate.rip_title_summary?.unfinished_titles ?? []).some((title) => title.drive_index === drive.drive_index)
              ));
              const earlierActiveJob = currentDiscJobs.find((candidate) => candidate.job_id !== job?.job_id && ['authorized', 'queued', 'running', 'pause_requested'].includes(candidate.state));
              const driveFingerprint = drive.current_disc_fingerprint ?? job?.preview?.jobs.map((item) => item.staging_destination.match(/(?:^|\/)([0-9a-f]{16})(?:\/|$)/)?.[1]).find((value): value is string => Boolean(value));
              const drivePipelineItems = (pipelineQueue?.items ?? []).filter((item) => driveFingerprint && item.disc_fingerprint === driveFingerprint);
              const preparedMatchingScope = driveFingerprint
                ? pipelineQueue?.disc_matching_scopes?.find((scope) => scope.disc_fingerprint === driveFingerprint)
                : undefined;
              const preparedRecoveryScope = driveFingerprint
                ? pipelineQueue?.disc_recovery_scopes?.find((scope) => scope.disc_fingerprint === driveFingerprint)
                : undefined;
              const latestDrivePipelineItems = Array.from(drivePipelineItems.reduce((items, item) => {
                if (typeof item.title_index !== 'number') return items;
                const previous = items.get(item.title_index);
                items.set(item.title_index, preferredPipelineItem(previous, item));
                return items;
              }, new Map<number, PipelineQueueItem>()).values());
              const discResultItems = [...latestDrivePipelineItems].sort((left, right) => (
                (left.title_index ?? Number.MAX_SAFE_INTEGER) - (right.title_index ?? Number.MAX_SAFE_INTEGER)
                || Date.parse(left.updated_at) - Date.parse(right.updated_at)
              ));
              const preparedFailureRecoveryJob = failedRipJob
                && job?.state === 'awaiting_review'
                && currentDiscJobs.some((candidate) => candidate.job_id === job.job_id)
                ? job
                : undefined;
              const inventoryPlanJob = preparedFailureRecoveryJob ?? [...currentDiscJobs]
                .filter((candidate) => candidate.preview?.jobs.some((item) => item.drive_index === drive.drive_index))
                .sort((left, right) => {
                  const leftCount = left.preview?.jobs.filter((item) => item.drive_index === drive.drive_index).length ?? 0;
                  const rightCount = right.preview?.jobs.filter((item) => item.drive_index === drive.drive_index).length ?? 0;
                  return rightCount - leftCount || Date.parse(right.created_at) - Date.parse(left.created_at);
                })[0] ?? job;
              const failedRecoveryPlanIsStale = Boolean(
                failedRipJob
                && !preparedRecoveryScope,
              );
              const expectedPipelineTitleIndexes = new Set(preparedRecoveryScope
                ? preparedRecoveryScope.required_title_indexes
                : failedRecoveryPlanIsStale
                  ? failedRipJob?.preview?.jobs
                    .filter((item) => item.drive_index === drive.drive_index)
                    .map((item) => item.title_index) ?? []
                  : preparedMatchingScope
                    ? preparedMatchingScope.relevant_title_indexes
                    : inventoryPlanJob?.preview?.jobs
                  .filter((item) => item.drive_index === drive.drive_index)
                  .map((item) => item.title_index) ?? []);
              const historicallyKnownTitleIndexes = preparedRecoveryScope || preparedFailureRecoveryJob
                ? new Set(expectedPipelineTitleIndexes)
                : new Set(currentDiscJobs.flatMap((candidate) => (
                    candidate.preview?.jobs
                      .filter((item) => item.drive_index === drive.drive_index)
                      .map((item) => item.title_index) ?? []
                  )));
              const organizedTitleIndexes = new Set(drivePipelineItems
                .filter((item) => item.stage === 'organize' && item.state === 'completed' && typeof item.title_index === 'number')
                .map((item) => item.title_index as number));
              const organizedExpectedTitleCount = [...expectedPipelineTitleIndexes]
                .filter((titleIndex) => organizedTitleIndexes.has(titleIndex)).length;
              const futureSkipApiReady = Array.isArray(pipelineQueue?.title_dispositions);
              const skippedTitleIndexes = new Set((pipelineQueue?.title_dispositions ?? [])
                .filter((item) => driveFingerprint && item.disc_fingerprint === driveFingerprint && item.disposition === 'skip')
                .map((item) => item.title_index));
              const skippedDiscTitles = (pipelineQueue?.title_dispositions ?? [])
                .filter((item) => driveFingerprint && item.disc_fingerprint === driveFingerprint && item.disposition === 'skip');
              const safelyPresentTitleIndexes = new Set([
                ...organizedTitleIndexes,
                ...drivePipelineItems
                  .filter((item) => item.pipeline_media_available && typeof item.title_index === 'number')
                  .map((item) => item.title_index as number),
                ...currentDiscJobs.flatMap((candidate) => (
                  candidate.preview?.jobs
                    .filter((item) => item.prior_library_status === 'present')
                    .map((item) => item.title_index) ?? []
                )),
              ]);
              const unavailableInventoryTitleIndexes = [...historicallyKnownTitleIndexes]
                .filter((titleIndex) => !expectedPipelineTitleIndexes.has(titleIndex)
                  && !safelyPresentTitleIndexes.has(titleIndex)
                  && !skippedTitleIndexes.has(titleIndex))
                .sort((left, right) => left - right);
              const missingInventoryTitleIndexes = [...expectedPipelineTitleIndexes]
                .filter((titleIndex) => !safelyPresentTitleIndexes.has(titleIndex) && !skippedTitleIndexes.has(titleIndex))
                .sort((left, right) => left - right);
              const inventoryRecoveryJob = missingInventoryTitleIndexes.length > 0
                && inventoryPlanJob?.state === 'awaiting_review'
                ? inventoryPlanJob
                : undefined;
              const currentReviewRecoveryJob = (job?.state === 'awaiting_review' || (job?.state === 'queued' && job.executor_attached === false))
                && safelyPresentTitleIndexes.size > 0
                && currentDiscJobs.some((candidate) => candidate.job_id === job.job_id)
                ? job
                : undefined;
              const reripPlanJob = inventoryRecoveryJob ?? failedRipJob ?? currentReviewRecoveryJob;
              const missingRipTitleIndexes = reripPlanJob?.preview?.jobs
                .filter((item) => (
                  item.drive_index === drive.drive_index
                  && expectedPipelineTitleIndexes.has(item.title_index)
                  && !safelyPresentTitleIndexes.has(item.title_index)
                  && !skippedTitleIndexes.has(item.title_index)
                ))
                .map((item) => item.title_index) ?? [];
              const reripJob = missingRipTitleIndexes.length > 0 ? reripPlanJob : undefined;
              const missingRipTitleCount = missingRipTitleIndexes.length;
              const recoverableStagedTitleIndexes = inventoryPlanJob?.job_id === selectedJobId
                ? recoverableExistingTitleIndexes.filter((titleIndex) => missingRipTitleIndexes.includes(titleIndex))
                : [];
              const missingAfterStagedRecoveryTitleIndexes = missingRipTitleIndexes
                .filter((titleIndex) => !recoverableStagedTitleIndexes.includes(titleIndex));
              const missingRipPreviewJobs = (reripPlanJob?.preview?.jobs ?? [])
                .filter((item) => item.drive_index === drive.drive_index && missingRipTitleIndexes.includes(item.title_index))
                .sort((left, right) => left.title_index - right.title_index);
              const recoveryKnownTitleIndexes = new Set([
                ...historicallyKnownTitleIndexes,
                ...safelyPresentTitleIndexes,
              ]);
              const recoveryKnownTitleCount = recoveryKnownTitleIndexes.size;
              const recoveryPresentTitleCount = [...recoveryKnownTitleIndexes]
                .filter((titleIndex) => safelyPresentTitleIndexes.has(titleIndex)).length;
              const recoverySkippedTitleCount = [...recoveryKnownTitleIndexes]
                .filter((titleIndex) => skippedTitleIndexes.has(titleIndex)).length;
              const driveRipComplete = expectedPipelineTitleIndexes.size > 0
                && unavailableInventoryTitleIndexes.length === 0
                && [...expectedPipelineTitleIndexes].every((titleIndex) => (
                  safelyPresentTitleIndexes.has(titleIndex) || skippedTitleIndexes.has(titleIndex)
                ));
              const completedDiscTitleCount = expectedPipelineTitleIndexes.size;
              const activeRipTitleMatch = job?.state === 'running' ? job.rip_progress_scope?.match(/title-(\d+)$/) : null;
              const activeRipTitleIndex = activeRipTitleMatch ? Number.parseInt(activeRipTitleMatch[1], 10) : null;
              const activeRipTitleIsSkipped = activeRipTitleIndex !== null && skippedTitleIndexes.has(activeRipTitleIndex);
              const failedRipTitleMatch = reripJob?.state === 'failed' ? reripJob.rip_progress_scope?.match(/title-(\d+)$/) : null;
              const failedRipTitleIndex = failedRipTitleMatch ? Number.parseInt(failedRipTitleMatch[1], 10) : null;
              const failedRipTitleIsSkipped = failedRipTitleIndex !== null && skippedTitleIndexes.has(failedRipTitleIndex);
              const identificationReviewItems = latestDrivePipelineItems.filter((item) => item.stage === 'identify' && ['failed', 'review_required'].includes(item.state));
              const identificationNeedsAttention = identificationReviewItems.length > 0;
              const identificationActiveItems = latestDrivePipelineItems.filter((item) => item.stage === 'identify' && ['queued', 'running'].includes(item.state));
              const queuedDiscItems = latestDrivePipelineItems.filter((item) => item.state === 'queued');
              const queuedMatchedDiscItems = queuedDiscItems.filter((item) => (
                item.stage === 'identify' && hasSavedIdentification(item)
              ));
              const identificationAwaitingFirstAttempt = identificationReviewItems.some((item) => (
                item.state === 'review_required'
                && ['missing_season_context', 'unmatched_disc_analysis_required'].includes(item.review_code || '')
                && (item.identification_attempts?.length ?? 0) === 0
              ));
              const identificationReviewTarget = identificationReviewItems[0];
              const outstandingDiscReviewItems = latestDrivePipelineItems.filter((item) => item.state === 'review_required');
              const outstandingDiscDownstreamItems = latestDrivePipelineItems.filter((item) => !['completed', 'discarded'].includes(item.state));
              const discardedIdentificationRemains = drivePipelineItems.some((item) => item.stage === 'identify' && item.state === 'discarded' && item.staged_source_available);
              const driveFailed = Boolean(reripJob) || (job?.state === 'failed' && (job.failed_drive_indexes?.length === 0 || job.failed_drive_indexes?.includes(drive.drive_index)));
              const driveAlreadyComplete = job?.state === 'awaiting_review' && allKnownTitlesAlreadyInLibrary(job.preview);
              const driveNeedsReview = job?.state === 'awaiting_review' && !driveAlreadyComplete;
              const drivePaused = job?.state === 'paused';
              const queuedWithoutExecutor = job?.state === 'queued' && job.executor_attached === false;
              const interruptedQueued = job?.state === 'queued' && job.executor_attached === false && job.rip_progress_percent !== null && job.rip_progress_percent !== undefined;
              const driveNeedsAction = Boolean(reripJob) || unavailableInventoryTitleIndexes.length > 0 || driveNeedsReview || drivePaused || job?.state === 'queued' || discardedIdentificationRemains || Boolean(earlierActiveJob);
              const driveStatus = discIdentityNeedsVerification
                ? 'disc identity needs verification'
                : reripJob
                  ? failedRecoveryPlanIsStale
                  ? 'recovery scope needs verification'
                  : recoverableStagedTitleIndexes.length > 0
                    ? `recover ${recoverableStagedTitleIndexes.length} preserved · ${missingAfterStagedRecoveryTitleIndexes.length} still missing`
                    : `rerip ${missingRipTitleCount} relevant missing ${missingRipTitleCount === 1 ? 'title' : 'titles'}`
                : driveRipComplete
                  ? identificationNeedsAttention
                    ? identificationAwaitingFirstAttempt
                      ? 'rip complete · matching waiting to start'
                      : 'rip complete · identification needs attention'
                    : identificationActiveItems.length > 0
                      ? `rip complete · identification ${identificationActiveItems.some((item) => item.state === 'running') ? 'running' : 'queued'}`
                    : outstandingDiscReviewItems.length > 0
                      ? `rip complete · ${outstandingDiscReviewItems.length} saved ${outstandingDiscReviewItems.length === 1 ? 'result needs' : 'results need'} review`
                      : skippedDiscTitles.length > 0
                        ? `rip complete with ${skippedDiscTitles.length} force-ignored ${skippedDiscTitles.length === 1 ? 'title' : 'titles'}`
                        : 'rip complete · all relevant titles safely present'
                : identificationNeedsAttention
                  ? 'identification needs attention'
                : outstandingDiscReviewItems.length > 0
                  ? `${outstandingDiscReviewItems.length} saved ${outstandingDiscReviewItems.length === 1 ? 'result needs' : 'results need'} review`
                : discardedIdentificationRemains
                  ? 'unidentified rip preserved'
                  : earlierActiveJob
                    ? 'eject held by earlier rip job'
                    : driveAlreadyComplete
                      ? 'already complete in Jellyfin'
                    : unavailableInventoryTitleIndexes.length > 0
                      ? `${unavailableInventoryTitleIndexes.length} previously seen ${unavailableInventoryTitleIndexes.length === 1 ? 'title is' : 'titles are'} missing from inventory`
                    : job?.state === 'completed'
                      ? expectedPipelineTitleIndexes.size > 0
                        ? `rip complete · ${organizedExpectedTitleCount} of ${expectedPipelineTitleIndexes.size} organized`
                        : 'rip complete · downstream processing continues'
                      : queuedWithoutExecutor
                        ? 'queued · waiting for rip worker'
                      : driveRipping
                        ? 'ripping'
                        : job?.state.replaceAll('_', ' ') ?? physicalDriveOperation ?? (drivePreparing ? 'MakeMKV is reading disc' : 'disc inserted');
              const selectedDriveJob = savedJob?.job_id === job?.job_id;
              const completeAutoEjectKey = job && driveRipComplete ? completeDiscAutoEjectKey(drive, job) : null;
              const completeAutoEjectDeadline = completeAutoEjectKey ? completeDiscAutoEjectDeadlines[completeAutoEjectKey] : undefined;
              const completeAutoEjectSeconds = completeAutoEjectDeadline === undefined
                ? null
                : Math.max(0, Math.ceil((completeAutoEjectDeadline - completeDiscAutoEjectClock) / 1000));
              const completeAutoEjectHold = completeAutoEjectKey ? completeDiscAutoEjectHolds[completeAutoEjectKey] : undefined;
              const stagingAttemptCollision = job?.state === 'awaiting_review'
                && (job.preview?.collision_count ?? 0) > 0
                && job.preview?.jobs.every((item) => ['clear', 'staging-exists'].includes(item.collision_status));
              const driveIndicatorClass = driveNeedsAction || driveFailed
                ? 'text-red-300'
                : driveRipping
                  ? 'text-blue-300'
                  : drivePreparing || discIdentityNeedsVerification || identificationNeedsAttention || outstandingDiscDownstreamItems.length > 0 || skippedDiscTitles.length > 0
                    ? 'text-amber-300'
                    : driveRipComplete
                      ? 'text-green-300'
                      : drive.has_disc
                        ? 'text-blue-300'
                        : 'text-slate-600';
              return (
                <div key={driveKey} className={`rounded-xl border p-4 space-y-4 ${driveNeedsAction || driveFailed
                  ? 'border-red-500/60 bg-red-500/10'
                  : driveRipping
                    ? 'border-blue-400/60 bg-blue-500/10'
                  : drivePreparing || discIdentityNeedsVerification || identificationNeedsAttention || outstandingDiscDownstreamItems.length > 0 || skippedDiscTitles.length > 0
                    ? 'border-amber-400/60 bg-amber-500/10'
                  : driveRipComplete
                    ? 'border-green-400/60 bg-green-500/10'
                  : selectedDriveJob
                    ? 'border-blue-400/60 bg-blue-500/10'
                    : 'border-[var(--border-color)] bg-[var(--bg-primary)]/40'
                }`}>
                  <div className="flex items-center justify-between gap-3">
                    <span className={`text-4xl ${driveIndicatorClass}`} aria-label={drive.has_disc ? 'Disc inserted' : mediaCheckPending ? 'Media check pending' : 'Empty tray'}>{drive.has_disc ? '●' : '▱'}</span>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2 font-semibold text-white">
                        <span>Optical drive {drive.drive_index + 1}{drive.display_name ? ` · ${drive.display_name}` : ''}{drive.disc_label ? ` — ${drive.disc_label}` : ''}</span>
                        {skippedDiscTitles.length > 0 && <span className="rounded-full border border-amber-300/50 bg-amber-400/15 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-amber-200">⚠ {skippedDiscTitles.length} force-ignored {skippedDiscTitles.length === 1 ? 'title' : 'titles'}</span>}
                      </div>
                      <div className={`text-xs font-bold uppercase ${driveNeedsAction || driveFailed ? 'text-red-300' : driveRipping ? 'text-blue-200' : drivePreparing || mediaCheckPending || discIdentityNeedsVerification || identificationNeedsAttention || outstandingDiscDownstreamItems.length > 0 || skippedDiscTitles.length > 0 ? 'text-amber-300' : driveRipComplete ? 'text-green-300' : drive.has_disc ? 'text-blue-300' : 'text-slate-400'}`}>{drive.has_disc ? driveStatus : mediaCheckPending ? 'media check pending · MakeMKV slot not confirmed' : 'empty tray'}</div>
                    </div>
                  </div>
                  {mediaCheckPending && (
                    <div className="rounded-lg border border-amber-400/30 bg-amber-500/10 p-3 text-sm text-amber-100">
                      Windows detected this optical drive. MakeMKV has not yet confirmed whether its tray contains a disc, so RipWeaver keeps the drive visible but will not start disc work.
                    </div>
                  )}
                  {drive.has_disc && <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <label className="space-y-1">
                      <span className="text-xs font-semibold text-white">Disc contains</span>
                      <select className="w-full rounded-lg bg-[var(--bg-primary)] border border-[var(--border-color)] p-2 text-sm text-white" value={setup.contentType} onChange={(event) => { void persistDiscSetup(drive, { contentType: event.target.value as DiscContentType }); }}>
                        <option value="">Automatic (no hint)</option>
                        <option value="tv">TV episodes</option>
                        <option value="movie">Movie</option>
                        <option value="extras">Extras / bonus disc</option>
                        <option value="mixed">Mixed main titles + extras</option>
                      </select>
                    </label>
                    <label className="space-y-1">
                      <span className="text-xs font-semibold text-white">HandBrake profile</span>
                      <select className="w-full rounded-lg bg-[var(--bg-primary)] border border-[var(--border-color)] p-2 text-sm text-white" value={setup.handbrakeProfile} onChange={(event) => { const profileId = event.target.value; if (profileId === '__custom__') { updateDiscSetup(driveKey, { handbrakeProfile: '' }); onOpenSettings?.(); return; } void persistDiscSetup(drive, { handbrakeProfile: profileId }); void rememberDefaultProfile(profileId); }}>
                        <option value="">Automatic by source resolution (general fallback: {setup.automaticProfile})</option>
                        <option value="__custom__">Custom… open profile settings</option>
                        {handbrakeProfiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.display_name}{profile.built_in ? '' : ' (custom)'}</option>)}
                      </select>
                    </label>
                  </div>}
                  {drive.has_disc && <div className="text-xs text-[var(--text-muted)]">Optional hints: leave blank for automatic classification and the default transcode profile. A content hint changes search priority but still permits fallback.</div>}
                  {drive.has_disc && pipelineQueue?.paused && queuedDiscItems.length > 0 && !reripJob && (
                    <div className="rounded-lg border border-blue-300/50 bg-blue-500/15 p-4 text-sm text-blue-50 space-y-3">
                      <div>
                        <div className="font-semibold">Next step: resume processing</div>
                        <div className="mt-1 text-xs text-blue-100/80">
                          {queuedMatchedDiscItems.length > 0
                            ? `${queuedMatchedDiscItems.length} ${queuedMatchedDiscItems.length === 1 ? 'title already has a saved episode match' : 'titles already have saved episode matches'}. `
                            : `${queuedDiscItems.length} ${queuedDiscItems.length === 1 ? 'title is' : 'titles are'} ready to continue. `}
                          RipWeaver is paused, so it cannot advance them. No rerip or manual naming is needed now.
                        </div>
                      </div>
                      <button type="button" className="btn btn-primary" disabled={controlling} onClick={() => controlPipeline('resume')}>
                        Resume processing
                      </button>
                      <details className="rounded border border-blue-200/20 bg-black/10 p-2 text-xs text-blue-100/75">
                        <summary className="cursor-pointer font-semibold text-blue-50">Other choices</summary>
                        <div className="mt-2">Leave processing paused if you are not ready for the queued work to continue. Matching logs and manual review remain available below if RipWeaver later asks for a decision.</div>
                      </details>
                    </div>
                  )}
                  {drive.has_disc && skippedDiscTitles.length > 0 && (
                    <div className="rounded-lg border border-amber-400/40 bg-amber-500/15 p-3 text-sm text-amber-100">
                      <div className="font-semibold">{skippedDiscTitles.length} disc {skippedDiscTitles.length === 1 ? 'title is' : 'titles are'} being forcefully ignored</div>
                      <div className="mt-1 text-xs text-amber-100/75">No staged MKV exists for an excluded unreadable title, so RipWeaver cannot identify or manually name it yet. Restore it to prepare another read attempt.</div>
                      <div className="mt-3 flex flex-wrap gap-2">
                        {skippedDiscTitles.map((skipped) => (
                          <button key={`${skipped.disc_fingerprint}-${skipped.title_index}`} type="button" className="btn btn-secondary text-xs" disabled={controlling || preparingDrive === drive.drive_index} onClick={() => restoreSkippedDiscTitle(skipped, drive, job)}>
                            Restore title {skipped.title_index} and prepare retry
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                  {drive.has_disc && drivePreparing && !job && (
                    <div className="rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-sm text-amber-100">
                      <div className="font-semibold">{physicalDriveOperation ?? 'MakeMKV is reading this drive'}</div>
                      <div className="mt-1 text-xs text-amber-100/75">RipWeaver has reserved this exact drive for the operation shown above. Other physical work waits until it finishes.</div>
                    </div>
                  )}
                  {drive.has_disc && job?.state === 'running' && (
                    <div className="rounded-lg border border-blue-400/30 bg-blue-400/10 p-3 text-sm text-blue-100">
                      <div className="flex items-center justify-between gap-3">
                        <span className="font-semibold">Whole disc</span>
                        <span>{job.rip_overall_progress_percent ?? 0}%{job.rip_completed_title_count !== null && job.rip_completed_title_count !== undefined ? ` · ${job.rip_completed_title_count} of ${job.rip_total_title_count ?? 0} titles finished` : ''}</span>
                      </div>
                      <div className="mt-2 h-2 overflow-hidden rounded bg-black/30">
                        <div className="h-full bg-blue-400 transition-all" style={{ width: `${job.rip_overall_progress_percent ?? 0}%` }} />
                      </div>
                      <div className="mt-3 flex items-center justify-between gap-3 text-xs text-blue-100/80">
                        <span>{job.rip_progress_scope === 'batch' || job.rip_progress_scope === 'batch-phase' ? 'Current MakeMKV phase' : 'Current title'}</span>
                        <span>{job.rip_progress_percent ?? 0}%{job.rip_transfer_mib_s ? ` · ${job.rip_transfer_mib_s.toFixed(2)} MiB/s` : ''}</span>
                      </div>
                      <div className="mt-1 h-1.5 overflow-hidden rounded bg-black/30">
                        <div className="h-full bg-cyan-300 transition-all" style={{ width: `${job.rip_progress_percent ?? 0}%` }} />
                      </div>
                      <div className="mt-1 text-xs text-blue-100/70">{job.rip_progress_scope && job.rip_progress_scope !== 'batch' ? `${job.rip_progress_scope} · ` : ''}rate appears after two MakeMKV progress samples.</div>
                      {(job.pipeline_queued_title_count ?? 0) > 0 && (
                        <div className="mt-2 rounded border border-cyan-300/30 bg-cyan-400/10 p-2 text-xs text-cyan-100">
                          {job.pipeline_queued_title_count} completed {job.pipeline_queued_title_count === 1 ? 'MKV is' : 'MKVs are'} already in the processing queue. Other drives continue ripping independently.
                        </div>
                      )}
                      {job.rip_possibly_stalled && (
                        <div className="mt-2 rounded border border-amber-400/40 bg-amber-500/15 p-2 text-xs text-amber-100">
                          <div>Possibly stalled: no MakeMKV activity was recorded for {formatActivityAge(job.rip_activity_age_seconds)}. RipWeaver has not stopped or restarted the process; MakeMKV may still be retrying a difficult read.</div>
                          {futureSkipApiReady && driveFingerprint && activeRipTitleIndex !== null && (
                            activeRipTitleIsSkipped ? (
                              <div className="mt-2 font-semibold">Title {activeRipTitleIndex} is saved to be skipped on future rip attempts. This active attempt is unchanged.</div>
                            ) : (
                              <button
                                type="button"
                                className="btn btn-secondary mt-2 text-xs"
                                disabled={controlling}
                                onClick={() => void skipDiscTitleAfterReadFailure(driveFingerprint, activeRipTitleIndex)}
                              >
                                Exclude title {activeRipTitleIndex} from future rerips
                              </button>
                            )
                          )}
                        </div>
                      )}
                    </div>
                  )}
                  {drive.has_disc && job?.pipeline_handoff_status === 'attention_required' && (
                    <div className="rounded-lg border border-amber-400/40 bg-amber-500/15 p-3 text-sm text-amber-100">
                      <div className="font-semibold">Rip output is safe; queue handoff needs attention</div>
                      <div className="mt-1 text-xs">MakeMKV completed independently. {job.pipeline_handoff_pending_title_count ?? 0} completed {job.pipeline_handoff_pending_title_count === 1 ? 'title still needs' : 'titles still need'} to be added to the processing queue; use “Add missing items from this disc” to recover them without reripping.</div>
                    </div>
                  )}
                  {drive.has_disc && (Boolean(reripJob) || identificationNeedsAttention) && (
                    <div className={`rounded-lg border p-3 text-sm space-y-2 ${reripJob?.state === 'failed' ? 'border-red-500/40 bg-red-500/15 text-red-100' : 'border-amber-400/40 bg-amber-500/15 text-amber-100'}`}>
                      {reripJob ? (
                        <>
                          <div className="font-semibold">
                            {failedRecoveryPlanIsStale
                              ? 'Remaining titles have not been verified as relevant'
                              : reripJob.state === 'failed'
                              ? `Rip stopped safely${reripJob.error_category ? ` · ${reripJob.error_category.replaceAll('_', ' ')}` : ''}`
                              : reripJob.state === 'queued'
                                ? 'Missing-title rerip is queued but has not started'
                                : 'Missing-title rerip ready'}
                          </div>
                          <div className="rounded border border-amber-300/30 bg-black/10 p-2">
                            <div className="font-semibold">Current recovery status: {recoveryPresentTitleCount} of {recoveryKnownTitleCount} titles are already safely present in staging or Jellyfin.</div>
                            {recoverableStagedTitleIndexes.length > 0 && (
                              <div className="mt-2 rounded border border-blue-300/30 bg-blue-500/10 p-2 text-blue-100">
                                <div className="font-semibold">{recoverableStagedTitleIndexes.length} completed staged title{recoverableStagedTitleIndexes.length === 1 ? ' is' : 's are'} preserved and awaiting read-only verification: {recoverableStagedTitleIndexes.join(', ')}.</div>
                                <div className="mt-1 text-xs">Do not rerip these titles. Verify and add them to identification first. After that, only {missingAfterStagedRecoveryTitleIndexes.length || 'no'} title{missingAfterStagedRecoveryTitleIndexes.length === 1 ? '' : 's'} remain unavailable{missingAfterStagedRecoveryTitleIndexes.length ? `: ${missingAfterStagedRecoveryTitleIndexes.join(', ')}` : ''}.</div>
                              </div>
                            )}
                            {recoverySkippedTitleCount > 0 && <div className="mt-1">{recoverySkippedTitleCount} {recoverySkippedTitleCount === 1 ? 'title is' : 'titles are'} intentionally skipped for this exact disc.</div>}
                            {failedRecoveryPlanIsStale ? (
                              <div className="mt-2 rounded border border-red-300/30 bg-red-500/10 p-2 text-red-100">
                                <div className="font-semibold">Recommendation: do not rerip titles {missingRipTitleIndexes.join(', ')} from this stale plan.</div>
                                <div className="mt-1 text-xs">They are unverified candidates, not confirmed content files. Refresh the recovery scope first; RipWeaver will then keep only titles the current inventory classifier can verify as relevant.</div>
                              </div>
                            ) : recoverableStagedTitleIndexes.length > 0 ? (
                              <div className="mt-2 font-semibold">Recommendation: recover the preserved titles before authorizing any rerip.</div>
                            ) : (
                              <>
                                <div className="mt-2 font-semibold">Recommendation: rip the {missingRipTitleCount} title{missingRipTitleCount === 1 ? '' : 's'} below.</div>
                                <div className="mt-1 text-xs">The refreshed inventory still classifies each one as relevant to this failed rip. Titles omitted by that refreshed classification are not rerip work.</div>
                                <div className="mt-2 space-y-1">
                                  {missingRipPreviewJobs.map((item) => (
                                    <div key={`rip-recommendation-${item.title_index}`} className="flex flex-wrap items-center justify-between gap-2 rounded border border-green-300/25 bg-green-500/10 px-2 py-1 text-xs">
                                      <span><strong>RIP title {item.title_index}</strong> · currently classified as relevant and not safely present</span>
                                      {formatBytes(item.estimated_bytes) && <span>{formatBytes(item.estimated_bytes)}</span>}
                                    </div>
                                  ))}
                                </div>
                              </>
                            )}
                          </div>
                          <div className="text-xs opacity-80">
                            {reripJob.state === 'failed'
                              ? 'Partials were preserved. The retry uses a new isolated attempt and never overwrites existing staged titles.'
                              : 'Existing staged titles will stay in identification. The missing-title rip uses a new isolated attempt and never overwrites them.'}
                          </div>
                          {reripJob.rip_title_summary && (
                            <details className="rounded border border-current/20 bg-black/15 p-2 text-xs">
                              <summary className="cursor-pointer font-semibold">Technical history from the failed attempt</summary>
                              <div className="mt-2">That attempt recorded {reripJob.rip_title_summary.verified_titles.length} of its {reripJob.rip_title_summary.total_titles} selected titles as newly verified before MakeMKV failed. This does not mean zero titles are currently available.</div>
                              <div className="mt-1">Newly verified by that attempt: {reripJob.rip_title_summary.verified_titles.length ? reripJob.rip_title_summary.verified_titles.map((title) => `title index ${title.title_index} (drive ${title.drive_index + 1})`).join(', ') : 'none'}</div>
                              <div className="mt-1">Not verified by that attempt: {reripJob.rip_title_summary.unfinished_titles.length ? reripJob.rip_title_summary.unfinished_titles.map((title) => `title index ${title.title_index} (drive ${title.drive_index + 1})`).join(', ') : 'none'}</div>
                            </details>
                          )}
                          {(reripJob.recommendations ?? []).map((recommendation) => <div key={recommendation} className="text-xs">• {recommendation}</div>)}
                          {futureSkipApiReady && reripJob.state === 'failed' && driveFingerprint && failedRipTitleIndex !== null && (
                            <div className="rounded border border-amber-300/30 bg-black/10 p-2 text-xs">
                              <div>MakeMKV last stopped while working on title {failedRipTitleIndex}. It remains included by default.</div>
                              {failedRipTitleIsSkipped ? (
                                <div className="mt-1 font-semibold">You chose to exclude title {failedRipTitleIndex} from future rerips for this exact disc.</div>
                              ) : (
                                <button
                                  type="button"
                                  className="btn btn-secondary mt-2 text-xs"
                                  disabled={controlling}
                                  onClick={() => void skipDiscTitleAfterReadFailure(driveFingerprint, failedRipTitleIndex)}
                                >
                                  Exclude title {failedRipTitleIndex} from future rerips
                                </button>
                              )}
                            </div>
                          )}
                        </>
                      ) : (
                        identificationAwaitingFirstAttempt
                          ? <div>Ripping finished, but one or more preserved titles have not received their first disc-wide matching attempt. Their MKVs are safe; run identification recovery, not MakeMKV.</div>
                          : <div>Ripping finished, but one or more verified titles still need identification review. Reripping those files would not improve their identification evidence.</div>
                      )}
                      <button
                        type="button"
                        className={reripJob ? 'btn btn-primary text-xs' : 'btn btn-secondary text-xs'}
                        disabled={Boolean(reripJob) && (
                          rerippingJobId === reripJob?.job_id
                          || preparingDrive === drive.drive_index
                          || Boolean(earlierActiveJob)
                        )}
                        onClick={() => reripJob
                          ? failedRecoveryPlanIsStale
                            ? void refreshFailedDiscRecovery(drive)
                            : recoverableStagedTitleIndexes.length > 0
                              ? selectDriveJob(reripJob)
                              : void reripMissingItems(reripJob, missingRipTitleIndexes)
                          : job && selectDriveJob(
                            job,
                            identificationReviewTarget
                              ? pipelineItemElementId(identificationReviewTarget.media_id)
                              : 'pipeline-review-actions',
                          )}
                      >
                        {reripJob
                          ? failedRecoveryPlanIsStale
                            ? preparingDrive === drive.drive_index
                              ? 'Refreshing failed-disc recovery…'
                              : 'Refresh failed-disc recovery'
                            : rerippingJobId === reripJob.job_id
                            ? 'Rerip request queued…'
                            : recoverableStagedTitleIndexes.length > 0
                            ? `Review ${recoverableStagedTitleIndexes.length} preserved title${recoverableStagedTitleIndexes.length === 1 ? '' : 's'} first`
                            : reripJob.state === 'queued'
                            ? `Start queued ${missingRipTitleCount}-title rerip`
                            : `Rerip ${missingRipTitleCount} missing ${missingRipTitleCount === 1 ? 'title' : 'titles'}`
                          : 'Review identification results'}
                      </button>
                    </div>
                  )}
                  {drive.has_disc && unavailableInventoryTitleIndexes.length > 0 && (
                    <div className="rounded-lg border border-red-400/40 bg-red-500/15 p-3 text-sm text-red-100 space-y-1">
                      <div className="font-semibold">Previously seen titles are missing from the current disc inventory</div>
                      <div className="text-xs">MakeMKV previously reported {unavailableInventoryTitleIndexes.length === 1 ? 'title' : 'titles'} {unavailableInventoryTitleIndexes.join(', ')}, but the latest complete inventory did not. RipWeaver cannot safely include {unavailableInventoryTitleIndexes.length === 1 ? 'it' : 'them'} in a rerip until MakeMKV reports {unavailableInventoryTitleIndexes.length === 1 ? 'that title' : 'those titles'} again.</div>
                      <div className="text-xs">Clean and reseat the disc, refresh it, or try another optical drive. Existing staged and Jellyfin files remain untouched.</div>
                    </div>
                  )}
                  {drive.has_disc && discardedIdentificationRemains && !identificationNeedsAttention && (
                    <div className="rounded-lg border border-amber-400/40 bg-amber-500/15 p-3 text-sm text-amber-100">
                      Unidentified titles were removed from the active queue, but their staged MKVs remain available in Recently Finished for deletion or later review.
                    </div>
                  )}
                  {drive.has_disc && earlierActiveJob && (
                    <div className="rounded-lg border border-red-400/40 bg-red-500/15 p-3 text-sm text-red-100 space-y-2">
                      <div>Automatic eject was held because an earlier rip job for this drive is still marked {earlierActiveJob.state.replaceAll('_', ' ')}.</div>
                      <button type="button" className="btn btn-secondary text-xs" onClick={() => selectDriveJob(earlierActiveJob)}>Review earlier rip job</button>
                    </div>
                  )}
                  {drive.has_disc && driveRipComplete && !reripJob && (
                    <div className="rounded-lg border border-green-400/40 bg-green-500/15 p-3 text-sm text-green-100 space-y-2">
                      <div className="font-semibold">Disc content is safely present</div>
                      <div className="text-xs text-green-100/80">
                        All {completedDiscTitleCount} substantial content {completedDiscTitleCount === 1 ? 'title is' : 'titles are'} safely present in staging or Jellyfin, or explicitly skipped. No additional MakeMKV rip is needed.
                        {outstandingDiscDownstreamItems.length > 0
                          ? ` ${outstandingDiscDownstreamItems.length} saved ${outstandingDiscDownstreamItems.length === 1 ? 'title still has' : 'titles still have'} identification, transcoding, organization, or review work remaining.`
                          : ' No downstream work remains for this disc.'}
                      </div>
                      {completeAutoEjectSeconds !== null && (
                        <div className="flex flex-wrap items-center justify-between gap-2 rounded border border-green-300/25 bg-black/10 p-2 text-xs">
                          <span>Automatically ejecting this completed disc in {completeAutoEjectSeconds} second{completeAutoEjectSeconds === 1 ? '' : 's'}.</span>
                          <button type="button" className="btn btn-secondary text-xs" onClick={() => completeAutoEjectKey && keepCompletedDiscInserted(completeAutoEjectKey)}>Keep disc inserted</button>
                        </div>
                      )}
                      {completeAutoEjectHold === 'kept' && <div className="text-xs text-green-100/80">Automatic eject was cancelled for this insertion.</div>}
                      {completeAutoEjectHold === 'failed' && (
                        <div className="text-xs text-amber-100">
                          Automatic eject was refused safely: {ejectFailureByDrive[drive.drive_index] || 'No additional Windows diagnostic was returned.'}
                        </div>
                      )}
                      {preparingDrive === drive.drive_index && (
                        <div className="rounded border border-amber-300/30 bg-amber-500/10 p-2 text-xs text-amber-100">A separate read-only recheck is still finishing. The completed pipeline result above remains valid.</div>
                      )}
                    </div>
                  )}
                  {drive.has_disc && (discResultItems.length > 0 || skippedDiscTitles.length > 0 || missingInventoryTitleIndexes.length > 0 || unavailableInventoryTitleIndexes.length > 0) && (
                    <details open={driveRipComplete || Boolean(failedRipJob) || undefined} className="rounded-lg border border-blue-400/30 bg-blue-500/10 p-3 text-sm text-blue-100">
                      <summary className="cursor-pointer font-semibold">All ripped titles and identification results ({discResultItems.length})</summary>
                      <div className="mt-3 space-y-2">
                        {missingInventoryTitleIndexes.length > 0 && (
                          <div className="rounded border border-amber-300/35 bg-amber-500/10 p-2 text-xs text-amber-100">Current inventory titles not safely present yet: {missingInventoryTitleIndexes.join(', ')}.</div>
                        )}
                        {unavailableInventoryTitleIndexes.length > 0 && (
                          <div className="rounded border border-red-300/35 bg-red-500/10 p-2 text-xs text-red-100">Previously reported but absent from the current inventory: {unavailableInventoryTitleIndexes.join(', ')}.</div>
                        )}
                        {discResultItems.map((item) => {
                          const skipped = typeof item.title_index === 'number' && skippedTitleIndexes.has(item.title_index);
                          const skipReason = skippedDiscTitles.find((candidate) => candidate.title_index === item.title_index)?.reason;
                          const attempts = item.identification_attempts ?? [];
                          const lastAttempt = attempts[attempts.length - 1];
                          const selectedEpisodeId = lastAttempt?.summary.selected_episode_id;
                          const selectedScore = lastAttempt?.summary.selected_score;
                          const engineThreshold = lastAttempt?.summary.engine_threshold;
                          const selectedVoteCount = lastAttempt?.summary.selected_vote_count;
                          const belowThreshold = item.review_code === 'episode_match_review'
                            && typeof selectedEpisodeId === 'string'
                            && typeof selectedScore === 'number'
                            && typeof engineThreshold === 'number';
                          const savedIdentificationWaiting = item.stage === 'identify'
                            && item.state === 'queued'
                            && hasSavedIdentification(item);
                          return (
                            <div key={item.media_id} className={`rounded border p-2 ${skipped ? 'border-slate-300/25 bg-slate-500/10' : item.state === 'review_required' ? 'border-amber-300/35 bg-amber-500/10' : item.state === 'completed' ? 'border-green-300/25 bg-green-500/10' : 'border-blue-300/20 bg-black/10'}`}>
                              <div className="font-semibold text-white">{typeof item.title_index === 'number' ? `Title ${item.title_index} · ` : ''}{skipped ? 'Excluded from episode matching' : item.display_name || 'Not identified'}</div>
                              <div className="mt-1 text-xs text-blue-100/75">
                                {skipped
                                  ? `not an episode · intentionally skipped${skipReason ? ` · ${skipReason.replaceAll('_', ' ')}` : ''}`
                                  : savedIdentificationWaiting
                                    ? `match saved · ${pipelineQueue?.paused ? 'resume processing to continue' : 'waiting to continue'}`
                                    : `${item.stage} · ${item.state.replaceAll('_', ' ')}${item.review_code ? ` · ${item.review_code.replaceAll('_', ' ')}` : ''}`}
                                {!skipped && attempts.length > 0 ? ` · ${attempts.length} matching attempts recorded` : ''}
                                {!skipped && item.state === 'review_required' && attempts.length === 0 ? ' · matching has not started' : ''}
                              </div>
                              {!skipped && item.state === 'queued' && (
                                <div className="mt-2 rounded border border-blue-300/25 bg-blue-500/10 p-2 text-xs text-blue-100">
                                  {item.stage === 'identify' && hasSavedIdentification(item)
                                    ? pipelineQueue?.paused
                                      ? 'This episode is already matched. Choose Resume processing to validate the saved result and continue.'
                                      : 'This episode is already matched. RipWeaver will validate the saved result and continue automatically.'
                                    : item.stage === 'identify'
                                      ? pipelineQueue?.paused
                                        ? 'This title is ready for episode identification. Choose Resume processing to start it.'
                                        : 'Queued to run episode identification.'
                                    : item.stage === 'transcode'
                                      ? 'Queued to transcode the identified title with HandBrake.'
                                      : item.stage === 'organize'
                                        ? 'Queued to place the verified encode in its Jellyfin destination.'
                                        : 'Queued for the next pipeline operation.'}
                                  {!pipelineQueue?.paused && ((pipelineQueue?.items ?? []).some((candidate) => candidate.media_id !== item.media_id && candidate.state === 'running' && candidate.stage === item.stage)
                                    ? ` Another ${item.stage} operation is running; this item will start automatically afterward.`
                                    : ' This stage can run alongside unrelated pipeline stages and should be claimed automatically.')}
                                </div>
                              )}
                              {belowThreshold && (
                                <div className="mt-2 rounded border border-amber-300/25 bg-amber-500/10 p-2 text-xs text-amber-100">
                                  Matching ran, but could not assign this title safely. Best candidate: {selectedEpisodeId} at {(selectedScore * 100).toFixed(1)}%; automatic threshold: {(engineThreshold * 100).toFixed(0)}%{typeof selectedVoteCount === 'number' ? `; qualifying windows: ${selectedVoteCount}` : ''}. Review or retry identification—do not rerip the MKV.
                                </div>
                              )}
                              {!skipped && item.state === 'review_required' && job && (
                                <button type="button" className="btn btn-secondary mt-2 text-xs" onClick={() => selectDriveJob(job, pipelineItemElementId(item.media_id))}>Open review</button>
                              )}
                            </div>
                          );
                        })}
                        {skippedDiscTitles.map((item) => (
                          <div key={`result-skip-${item.disc_fingerprint}-${item.title_index}`} className="rounded border border-amber-300/35 bg-amber-500/10 p-2">
                            <div className="font-semibold text-amber-50">Title {item.title_index} · forcefully ignored</div>
                            <div className="mt-1 text-xs font-semibold text-amber-100">Recommendation: do not rip unless you intentionally restore this title for another relevance/read test.</div>
                            <div className="mt-1 text-xs text-amber-100/75">Reason: {item.reason.replaceAll('_', ' ')}</div>
                          </div>
                        ))}
                      </div>
                    </details>
                  )}
                  {drive.has_disc && driveAlreadyComplete && !driveRipComplete && job?.preview && (
                    <div className="rounded-lg border border-green-400/40 bg-green-500/15 p-3 text-sm text-green-100 space-y-2">
                      <div className="font-semibold">Nothing needs to be ripped</div>
                      <div className="mt-1 text-xs text-green-100/80">All {new Set(job.preview.held_titles?.map((item) => item.title_index) ?? []).size} known episode destinations from this disc already exist in Jellyfin. RipWeaver did not start MakeMKV.</div>
                      {completeAutoEjectSeconds !== null && (
                        <div className="flex flex-wrap items-center justify-between gap-2 rounded border border-green-300/25 bg-black/10 p-2 text-xs">
                          <span>Automatically ejecting this completed disc in {completeAutoEjectSeconds} second{completeAutoEjectSeconds === 1 ? '' : 's'}.</span>
                          <button type="button" className="btn btn-secondary text-xs" onClick={() => completeAutoEjectKey && keepCompletedDiscInserted(completeAutoEjectKey)}>Keep disc inserted</button>
                        </div>
                      )}
                      {completeAutoEjectHold === 'kept' && <div className="text-xs text-green-100/80">Automatic eject was cancelled for this insertion.</div>}
                      {completeAutoEjectHold === 'failed' && <div className="text-xs text-amber-100">Automatic eject was refused safely: {ejectFailureByDrive[drive.drive_index] || 'No additional Windows diagnostic was returned.'}</div>}
                    </div>
                  )}
                  {drive.available && (
                    <button type="button" className="btn btn-secondary" disabled={ejectingDrives.includes(drive.drive_index) || queuedEjectDrives.includes(drive.drive_index) || ['authorized', 'queued', 'running', 'pause_requested'].includes(job?.state || '')} onClick={() => { if (completeAutoEjectKey) keepCompletedDiscInserted(completeAutoEjectKey); void ejectDrive(drive); }}>
                      {ejectingDrives.includes(drive.drive_index) ? (drive.has_disc ? 'Ejecting…' : 'Opening tray…') : queuedEjectDrives.includes(drive.drive_index) ? (drive.has_disc ? 'Queued to eject' : 'Queued to open') : drive.has_disc ? 'Eject disc' : 'Open tray'}
                    </button>
                  )}
                  {drive.has_disc && !driveRipComplete && (
                    <label className="block rounded-lg border border-blue-500/25 bg-blue-500/10 p-3 text-sm text-blue-100">
                      <input type="checkbox" className="mr-2" checked={setup.addMissingOnly} onChange={(event) => updateDiscSetup(driveKey, { addMissingOnly: event.target.checked })} />
                      Add missing items from this disc
                      <span className="mt-1 block text-xs text-blue-100/70">Check known results during preparation and check Jellyfin again immediately after every match. Existing destinations are held individually for review while missing episodes, extras, editions, commentary variants, and unrelated titles continue.</span>
                    </label>
                  )}
                  {drive.has_disc && !driveRipComplete && !reripJob && !identificationNeedsAttention && !discardedIdentificationRemains && (!job || ['completed', 'failed'].includes(job.state) || stagingAttemptCollision) && (
                    <button
                      type="button"
                      className="btn btn-primary w-full"
                      disabled={driveBusy || preparingDrive === drive.drive_index || queuedPrepareDrives.includes(drive.drive_index)}
                      onClick={() => queueDrivePipeline(drive, setup)}
                    >
                      {driveBusy
                        ? 'MakeMKV busy — please wait…'
                        : preparingDrive === drive.drive_index
                        ? 'Reading disc and preparing plan…'
                        : queuedPrepareDrives.includes(drive.drive_index)
                          ? 'Queued for preparation'
                          : stagingAttemptCollision
                            ? 'Prepare fresh isolated attempt'
                            : discIdentityNeedsVerification
                              ? 'Verify disc identity and restore saved status'
                            : job
                              ? 'Prepare a new pipeline for this disc'
                              : jobDashboard?.automatic_processing_enabled
                                ? 'Retry automatic preparation'
                                : 'Start pipeline for this disc'}
                    </button>
                  )}
                  {drive.has_disc && job && <div className="flex items-center justify-between text-xs">
                    {pipelineStages.map((stage, index) => {
                      const active = job.state === 'completed' || (stage === 'rip' && ['running', 'queued'].includes(job.state));
                      return <div key={stage} className={`flex items-center ${active ? 'text-green-300' : 'text-[var(--text-muted)]'}`}><span className="mr-1">{stageIcon[stage]}</span>{stage}{index < pipelineStages.length - 1 ? <span className="ml-2">→</span> : null}</div>;
                    })}
                  </div>}
                  {drive.has_disc && job?.preview && (!reripJob || ['running', 'pause_requested', 'paused'].includes(job.state)) && (['awaiting_review', 'authorized', 'queued', 'running', 'pause_requested', 'paused', 'failed'].includes(job.state) || identificationNeedsAttention || discardedIdentificationRemains) && (
                    <button type="button" className={driveNeedsReview || driveFailed ? 'btn btn-primary w-full' : 'btn btn-secondary w-full'} onClick={() => selectDriveJob(job)}>
                      {selectedDriveJob
                        ? 'This disc is shown below'
                        : ['running', 'pause_requested'].includes(job.state)
                          ? 'View current disc'
                          : job.state === 'paused'
                            ? 'View paused disc'
                        : driveAlreadyComplete
                          ? 'View completed-disc options'
                          : driveNeedsReview
                            ? 'Review this disc'
                        : driveFailed
                              ? 'Review error and retry options'
                              : queuedWithoutExecutor
                                ? 'Review queued rip'
                                : 'Open this disc’s controls'}
                    </button>
                  )}
                  {drive.has_disc && queuedWithoutExecutor && !interruptedQueued && !reripJob && job && (
                    <div className="rounded-lg border border-amber-400/40 bg-amber-500/15 p-3 space-y-1">
                      <div className="font-semibold text-amber-100">Queued and waiting for a rip worker</div>
                      <div className="text-xs text-amber-100/80">No MakeMKV process has claimed this saved request yet. Automatic restart recovery checks it after the startup safety pause. If it remains here, use “Review queued rip” to inspect the exact title list and start it with the required confirmation.</div>
                    </div>
                  )}
                  {drive.has_disc && drive.current_disc_fingerprint && job && !['authorized', 'queued', 'running', 'pause_requested', 'paused'].includes(job.state) && (
                    <div className="space-y-2">
                      <button type="button" className="btn btn-secondary w-full text-xs" disabled={controlling} onClick={() => void forgetDiscIdentity(drive)}>
                        Forget saved disc identity and matching history
                      </button>
                      <button type="button" className="btn w-full text-xs border border-red-400/50 bg-red-500/15 text-red-100 hover:bg-red-500/25" disabled={controlling} onClick={() => void forgetDiscIdentity(drive, true)}>
                        Forget identity and permanently delete staged MKVs
                      </button>
                    </div>
                  )}
                  {drive.has_disc && (drivePaused || interruptedQueued) && job && (
                    <div className="rounded-lg border border-amber-400/40 bg-amber-500/15 p-3 space-y-2">
                      <div className="font-semibold text-amber-100">Interrupted rip needs recovery</div>
                      <div className="text-xs text-amber-100/80">Recovery removes only exact incomplete MKVs and empty title folders from the interrupted attempt, then starts the reviewed titles again. Verified outputs and Jellyfin files remain protected.</div>
                      <button type="button" className="btn btn-primary" disabled={controlling} onClick={() => void reripMissingItems(job)}>Rerip unfinished items</button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      <details className="hidden">
        <summary className="cursor-pointer font-semibold text-white">Advanced: preview a saved MakeMKV report</summary>
        <div className="text-sm text-[var(--text-muted)] mt-2">Use this only to test or recover a plan from an already-saved, sanitized preflight report. It does not scan drives or start a rip.</div>
      <form onSubmit={handlePreview} className="mt-5 space-y-5">
        <div>
          <div className="font-bold text-white">Manual saved-report preview</div>
          <div className="text-sm text-[var(--text-muted)]">Fallback setup while live drive discovery is unavailable.</div>
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
          <label className="space-y-2">
            <span className="text-sm font-semibold text-white">Saved preflight reports</span>
            <textarea
              className="w-full min-h-32 rounded-xl bg-[var(--bg-primary)] border border-[var(--border-color)] p-3 text-sm font-mono text-white"
              value={reportText}
              onChange={(event) => setReportText(event.target.value)}
              placeholder="One explicit report.json path per line"
            />
            <span className="block text-xs text-[var(--text-muted)]">
              Reports are read explicitly; directories are never searched.
            </span>
          </label>

          <div className="space-y-4">
            <label className="block space-y-2">
              <span className="text-sm font-semibold text-white">Canonical series name</span>
              <input
                className="w-full rounded-xl bg-[var(--bg-primary)] border border-[var(--border-color)] p-3 text-white"
                value={seriesName}
                onChange={(event) => setSeriesName(event.target.value)}
                placeholder="Example Show"
              />
            </label>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <label className="block space-y-2">
                <span className="text-sm font-semibold text-white">Season</span>
                <input
                  className="w-full rounded-xl bg-[var(--bg-primary)] border border-[var(--border-color)] p-3 text-white"
                  value={season}
                  onChange={(event) => setSeason(event.target.value)}
                  placeholder="Blank for Unmatched"
                />
              </label>
              <label className="block space-y-2">
                <span className="text-sm font-semibold text-white">Output root for collision check and durable job</span>
                <input
                  className="w-full rounded-xl bg-[var(--bg-primary)] border border-[var(--border-color)] p-3 text-white"
                  value={outputRoot}
                  onChange={(event) => setOutputRoot(event.target.value)}
                  placeholder="Required before saving a job"
                />
              </label>
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="text-xs text-amber-300">
            Previewing and saving remain read-only. Physical execution requires a separate exact confirmation.
          </div>
          <button type="submit" className="btn btn-primary" disabled={loading}>
            {loading ? 'Building preview…' : 'Build read-only preview'}
          </button>
        </div>
      </form>
      </details>

      {error && (
        <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-red-200">
          {error}
        </div>
      )}

      </div>

      {pipelineQueue && (
        <div className="glass-panel rounded-xl p-5 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="font-bold text-white">{attentionOnly ? 'Pipeline errors and review choices' : queueOnly ? 'Downstream queue' : 'Selected disc queue'}</div>
              <div className="text-sm text-[var(--text-muted)]">
                {attentionOnly ? 'Items that need a decision are collected here without occupying an optical drive or blocking unrelated work.' : 'Each stage handles one item at a time, while unrelated stages may overlap. Identification runs automatically; transcoding and organization use validated automatic or reviewed authorization.'}
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              {!queueOnly && !attentionOnly && visiblePipelineItems.length === 0 && savedJob?.state === 'queued' ? (
                <button type="button" className="btn btn-primary" onClick={() => document.getElementById('rip-execution-confirmation')?.scrollIntoView({ behavior: 'smooth', block: 'center' })}>
                  Continue to rip confirmation
                </button>
              ) : (
                <>
                  {!attentionOnly && pipelineQueue.paused && (
                    <button type="button" className="btn btn-primary" disabled={controlling} onClick={() => controlPipeline('resume')}>
                      Resume processing
                    </button>
                  )}
                  {!attentionOnly && !pipelineQueue.paused && (
                    <button type="button" className="btn btn-secondary" disabled={controlling} onClick={() => controlPipeline('pause')}>
                      Pause after active work
                    </button>
                  )}
                  {(likelyRemovableCount > 0 || showLikelyRemovableOnly || visiblePipelineItems.some((item) => ['failed', 'review_required', 'queued'].includes(item.state))) && (
                    <details className="rounded-lg border border-[var(--border-color)] bg-black/10 px-3 py-2 text-sm">
                      <summary className="cursor-pointer font-semibold text-white">Manual queue options</summary>
                      <div className="mt-2 flex flex-wrap gap-2">
                        {(likelyRemovableCount > 0 || showLikelyRemovableOnly) && (
                          <button type="button" className={showLikelyRemovableOnly ? 'btn btn-primary' : 'btn btn-secondary'} onClick={() => setShowLikelyRemovableOnly((current) => !current)}>
                            Likely removable ({likelyRemovableCount})
                          </button>
                        )}
                        {visiblePipelineItems.some((item) => ['failed', 'review_required'].includes(item.state)) && (
                          <button type="button" className="btn btn-secondary" disabled={controlling} onClick={() => dismissPipelineItems(visiblePipelineItems.filter((item) => ['failed', 'review_required'].includes(item.state)).map((item) => item.media_id))}>
                            Clear held items
                          </button>
                        )}
                        {visiblePipelineItems.some((item) => item.state === 'queued') && (
                          <button type="button" className="btn btn-secondary" disabled={controlling} onClick={() => cancelQueuedPipelineItems(visiblePipelineItems.filter((item) => item.state === 'queued').map((item) => item.media_id))}>
                            Remove waiting items
                          </button>
                        )}
                      </div>
                    </details>
                  )}
                </>
              )}
            </div>
          </div>
          {pipelineQueue.paused && (
            <div className="rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-sm text-amber-100">
              {pipelineQueue.startup_resume_in_seconds
                ? `RipWeaver is in its startup safety delay and will reactivate processing in about ${pipelineQueue.startup_resume_in_seconds} seconds. Choose Pause active queue to keep it held, or Resume queue to continue immediately.`
                : 'Processing is paused by a fresh queue decision. Active MakeMKV or HandBrake work is allowed to settle safely; choose Resume queue when processing should continue.'}
            </div>
          )}
          {visibleRipJobs.length > 0 && (
            <div className="space-y-2 rounded-lg border border-blue-400/30 bg-blue-400/10 p-3 text-sm text-blue-100">
              <div className="font-semibold">Active MakeMKV rip work</div>
              {visibleRipJobs.map((job) => (
                <div key={job.job_id} className="rounded border border-blue-300/20 bg-black/10 p-3">
                  <div>{job.state === 'running' ? `MakeMKV is ripping now${job.rip_overall_progress_percent !== null && job.rip_overall_progress_percent !== undefined ? ` · whole disc ${job.rip_overall_progress_percent}%` : ''}${job.rip_progress_percent !== null && job.rip_progress_percent !== undefined ? ` · current title ${job.rip_progress_percent}%` : ''}${job.rip_transfer_mib_s ? ` · ${job.rip_transfer_mib_s.toFixed(2)} MiB/s` : ''}.` : job.state === 'pause_requested' ? 'The rip will pause safely after active work settles.' : 'The reviewed rip is waiting to start.'}</div>
                  <div className="mt-1 text-xs text-blue-100/75">
                    {job.preview?.jobs.length ?? 0} reviewed title(s). Each title enters identification only after its MKV finishes and verifies, so the downstream list may remain unchanged during a long title.
                  </div>
                  {job.rip_possibly_stalled && (
                    <div className="mt-2 rounded border border-amber-400/40 bg-amber-500/15 p-2 text-xs text-amber-100">
                      Possibly stalled: no MakeMKV activity was recorded for {formatActivityAge(job.rip_activity_age_seconds)}. RipWeaver has not stopped or restarted the process.
                    </div>
                  )}
                  <div className="mt-2 flex flex-wrap gap-1">
                    {(job.preview?.jobs ?? []).map((ripItem) => <span key={`${job.job_id}-${ripItem.title_index}`} className="rounded border border-blue-300/30 px-2 py-1 text-xs">Title {ripItem.title_index} · rip pending/active</span>)}
                  </div>
                </div>
              ))}
            </div>
          )}
          {!attentionOnly && visibleMatchingPerformance.length > 0 && (
            <details className="border-y border-[var(--border-color)] py-3 text-sm">
              <summary className="cursor-pointer font-semibold text-white">Recent matching performance</summary>
              <div className="mt-3 divide-y divide-[var(--border-color)]">
                {visibleMatchingPerformance.map((run) => (
                  <div key={run.run_id} className="grid gap-1 py-2 md:grid-cols-[minmax(12rem,1fr)_auto_auto] md:items-center md:gap-4">
                    <div>
                      <div className="font-medium text-white">{run.series_name}{run.outcome === 'failed' ? ' · failed' : ''}</div>
                      <div className="text-xs text-[var(--text-muted)]">
                        {run.anchor_count} anchor(s) of {run.title_count} titles · {run.season_scope.length > 0 ? `Season ${run.season_scope.join(', ')}` : 'all seasons'}
                      </div>
                      {run.outcome === 'failed' && <div className="text-xs text-red-200">{run.failure_stage || 'analysis'} · {run.failure_code || 'unknown failure'}</div>}
                      {run.provider_branches.length > 0 && <div className="text-[11px] text-[var(--text-muted)]">Tried {run.provider_branches.join(' → ')}</div>}
                    </div>
                    <div className="text-xs text-[var(--text-muted)]">{run.outcome === 'failed' ? `${run.unresolved_count} unresolved when stopped` : `${run.applied_count} matched · ${run.unresolved_count} unresolved`}</div>
                    <div className="text-xs text-[var(--text-muted)]">anchors {(run.anchor_elapsed_ms / 1000).toFixed(1)}s · total {(run.total_elapsed_ms / 1000).toFixed(1)}s</div>
                  </div>
                ))}
              </div>
            </details>
          )}
          {!queueOnly && !attentionOnly && visiblePipelineItems.some(requiresDiscWideTvRecovery) && (
            <div className="rounded-lg border border-indigo-400/30 bg-indigo-400/10 p-3 text-sm text-indigo-100 space-y-2">
              <div className="font-semibold">{suggestedSeason === null ? 'Run general all-season episode matching' : `Run Season ${suggestedSeason} episode matching`}</div>
              <p>{suggestedSeason === null ? 'The disc has no reliable season context. The matcher will compare each title independently against aired episodes for the reviewed series.' : `The disc label explicitly identifies Season ${suggestedSeason}. The matcher will restrict candidates to that season.`} A consecutive sequence can suggest what to inspect next, but it cannot name an episode or skip subtitle and Gemini checks.</p>
              <label className="block text-xs text-indigo-100">Canonical TV series name
                <input
                  className="mt-1 w-full rounded-lg bg-[var(--bg-primary)] border border-indigo-400/30 p-2 text-white"
                  value={unmatchedSeriesName}
                  onChange={(event) => setUnmatchedSeriesName(event.target.value)}
                  placeholder={suggestedUnmatchedSeries || 'Enter the TV series name'}
                />
              </label>
              {suggestedSeason !== null && (
                <div className="rounded-lg border border-indigo-300/20 p-3 space-y-2">
                  <div className="text-xs font-semibold">Optional reviewed episode range from the disc paperwork</div>
                  <div className="grid grid-cols-2 gap-2">
                    <label className="text-xs">First episode
                      <input
                        type="number"
                        min="1"
                        max="999"
                        className="mt-1 w-full rounded-lg bg-[var(--bg-primary)] border border-indigo-400/30 p-2 text-white"
                        value={unmatchedEpisodeStart}
                        onChange={(event) => setUnmatchedEpisodeStart(event.target.value)}
                        placeholder="20"
                      />
                    </label>
                    <label className="text-xs">Last episode
                      <input
                        type="number"
                        min="1"
                        max="999"
                        className="mt-1 w-full rounded-lg bg-[var(--bg-primary)] border border-indigo-400/30 p-2 text-white"
                        value={unmatchedEpisodeEnd}
                        onChange={(event) => setUnmatchedEpisodeEnd(event.target.value)}
                        placeholder="24"
                      />
                    </label>
                  </div>
                  <p className="text-[11px] text-indigo-100/75">This only limits which episode references may be tested. It never assigns an episode from title order or range position; each title still needs independent evidence.</p>
                </div>
              )}
              {learnedCoverage && learnedCoverage.disc_count > 0 && (
                <details className="border-y border-indigo-300/20 py-2 text-xs">
                  <summary className="cursor-pointer font-semibold">Learned fingerprint coverage · {learnedCoverage.disc_count} disc(s), {learnedCoverage.episode_count} episode(s)</summary>
                  <div className="mt-2 divide-y divide-indigo-300/15">
                    {learnedCoverage.discs.map((disc) => (
                      <div key={disc.disc_fingerprint} className="grid gap-1 py-2 sm:grid-cols-[9rem_1fr]">
                        <div className="font-mono">{disc.disc_fingerprint}{disc.disc_fingerprint === selectedDiscFingerprint ? ' · this disc' : ''}</div>
                        <div>
                          {disc.episode_count} episode(s){disc.seasons.length > 0 ? ` · Season ${disc.seasons.join(', ')}` : ''}{disc.other_title_count > 0 ? ` · ${disc.other_title_count} other learned title(s)` : ''}
                        </div>
                      </div>
                    ))}
                  </div>
                </details>
              )}
              <label className="flex items-start gap-2 text-xs text-indigo-100">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={discGeminiFallback}
                  onChange={(event) => setDiscGeminiFallback(event.target.checked)}
                />
                <span>Use Gemini when local sequence matching remains ambiguous. Bounded transcript excerpts and candidate episode metadata may be sent externally.</span>
              </label>
              <div className="flex flex-wrap gap-2">
                <button type="button" className="btn btn-primary" disabled={controlling} onClick={runAllSeasonAnalysis}>Analyze as TV {suggestedSeason === null ? 'series (all seasons)' : `series (Season ${suggestedSeason})`}</button>
                <button type="button" className="btn btn-secondary" disabled={controlling} onClick={classifyAsMovieExtras}>Analyze as movie / TV-movie bonus features</button>
              </div>
            </div>
          )}
          {pipelineQueue?.automatic_processing_enabled && (runningTranscodeItems.length > 0 || queuedTranscodeItems.length > 0) && (
            <div className="rounded-lg border border-emerald-400/30 bg-emerald-400/10 p-3 text-sm text-emerald-100 space-y-1">
              <div className="font-semibold">
                {runningTranscodeItems.length > 0 ? 'HandBrake is running' : 'HandBrake work is queued automatically'}
              </div>
              <div>
                {runningTranscodeItems.length > 0 && `${runningTranscodeItems[0].display_name || runningTranscodeItems[0].media_id} is encoding. `}
                {queuedTranscodeItems.length > 0
                  ? `${queuedTranscodeItems.length} ${queuedTranscodeItems.length === 1 ? 'title is' : 'titles are'} waiting in the automatic resolution-aware batch. `
                  : 'This is the final title currently assigned to HandBrake. '}
                No additional profile approval is needed.
              </div>
            </div>
          )}
          {!pipelineQueue?.automatic_processing_enabled && queuedTranscodeItems.length > 0 && (
            <div className="rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-sm text-amber-100 space-y-3">
              <p>Transcode jobs are ready but held until their exact HandBrake profile, encoded staging destination, and item set are reviewed.</p>
              <label className="block space-y-1">
                <span className="font-medium">Assign a HandBrake profile to this queued batch</span>
                <select className="input-field w-full" value={transcodeProfile || 'auto-resolution'} onChange={(event) => { setTranscodeProfile(event.target.value); setTranscodePlan(null); }}>
                  <option value="auto-resolution">Automatic by source resolution</option>
                  {handbrakeProfiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.display_name}</option>)}
                </select>
              </label>
              <div className="flex flex-wrap gap-2">
                <button type="button" className="btn btn-primary" onClick={reviewTranscodeAuthorization}>
                  Assign profile and review queued batch
                </button>
                {onOpenSettings && <button type="button" className="btn btn-secondary" onClick={onOpenSettings}>HandBrake settings</button>}
              </div>
            </div>
          )}
          {transcodePlan && !pipelineQueue?.automatic_processing_enabled && (
            <div className="rounded-lg border border-indigo-400/30 bg-indigo-400/10 p-4 space-y-3 text-sm">
              <div className="font-semibold text-white">Reviewed transcode batch</div>
              <div>{transcodePlan.item_count} exact queued item(s)</div>
              <div>Profile: {transcodePlan.profile_display_name} ({transcodePlan.default_profile_id})</div>
              <div>Destination: configured encoded staging root</div>
              <div className="font-mono text-xs break-all text-[var(--text-muted)]">Plan: {transcodePlan.plan_sha256}</div>
              <div className="text-amber-100">This starts HandBrake and FFprobe verification. It does not overwrite existing output or organize files into Jellyfin.</div>
              <div className="flex flex-wrap gap-2">
                <button type="button" className="btn btn-primary" disabled={controlling} onClick={authorizeTranscode}>
                  Start this exact transcode batch
                </button>
                <button type="button" className="btn btn-secondary" disabled={controlling} onClick={() => setTranscodePlan(null)}>
                  Cancel
                </button>
              </div>
            </div>
          )}
          {pipelineQueue?.automatic_organization_enabled && (runningOrganizationItems.length > 0 || queuedOrganizationItems.length > 0) && (
            <div className="rounded-lg border border-emerald-400/30 bg-emerald-400/10 p-3 text-sm text-emerald-100 space-y-1">
              <div className="font-semibold">Automatic Jellyfin placement</div>
              <div>
                {runningOrganizationItems.length > 0 ? 'A verified encode is being placed now. ' : ''}
                {queuedOrganizationItems.length > 0
                  ? `${queuedOrganizationItems.length} verified ${queuedOrganizationItems.length === 1 ? 'encode is' : 'encodes are'} waiting for collision-safe placement. `
                  : ''}
                Different resolution versions may coexist; an exact or same-resolution destination stops only that item for review.
              </div>
            </div>
          )}
          {!pipelineQueue?.automatic_organization_enabled && queuedOrganizationItems.length > 0 && !organizationPlan && (
            <div className="rounded-lg border border-blue-400/30 bg-blue-400/10 p-3 text-sm text-blue-100 space-y-3">
              <p>Verified encodes are ready in staging. Review their exact Jellyfin destinations and confirm a collision-refusing move.</p>
              <button type="button" className="btn btn-primary" disabled={controlling} onClick={reviewOrganizationAuthorization}>
                Review placement into Jellyfin
              </button>
            </div>
          )}
          {organizationPlan && !pipelineQueue?.automatic_organization_enabled && (
            <div className="rounded-lg border border-indigo-400/30 bg-indigo-400/10 p-4 space-y-3 text-sm">
              <div className="font-semibold text-white">Reviewed Jellyfin placement</div>
              <div>{organizationPlan.item_count} exact verified encode(s): {organizationPlan.tv_count} TV, {organizationPlan.movie_count} movie/bonus feature.</div>
              <div>{organizationPlan.collision_count === 0 ? 'No destination collisions detected.' : `${organizationPlan.collision_count} destination collision(s) require separate review.`}</div>
              <div className="max-h-48 overflow-y-auto rounded-lg border border-indigo-300/20 bg-black/10 p-2 space-y-1">
                {organizationPlan.items.map((item) => (
                  <div key={item.media_id} className={item.collision ? 'text-red-200' : 'text-indigo-100'}>
                    {item.destination_relative}{item.collision ? ' — collision' : ''}
                  </div>
                ))}
              </div>
              <div className="font-mono text-xs break-all text-[var(--text-muted)]">Plan: {organizationPlan.plan_sha256}</div>
              <div className="text-amber-100">This moves verified encodes from staging into Jellyfin. Existing destinations are never overwritten.</div>
              <div className="flex flex-wrap gap-2">
                <button type="button" className="btn btn-primary" disabled={controlling || organizationPlan.collision_count > 0} onClick={authorizeOrganization}>
                  Move these verified files into Jellyfin
                </button>
                <button type="button" className="btn btn-secondary" disabled={controlling} onClick={() => setOrganizationPlan(null)}>Cancel</button>
              </div>
            </div>
          )}
          {visiblePipelineItems.some((item) => item.review_code === 'library_collision') && (
            <div className="rounded-lg border border-red-400/30 bg-red-400/10 p-3 text-sm text-red-100 space-y-2">
              <div className="font-semibold">Existing Jellyfin episode found</div>
              <p>These items are intentionally held. Starting or resuming the queue cannot overwrite or bypass this review. The match was checked against Jellyfin before HandBrake for new work; older jobs that had already encoded remain safely held here.</p>
            </div>
          )}
          {visiblePipelineItems.some((item) => item.state === 'review_required' && item.review_code !== 'all_season_analysis_running') && (
            <div className="rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-sm text-amber-100 space-y-2">
              <div className="font-semibold">Review choices required</div>
              {visiblePipelineItems.some(requiresDiscWideTvRecovery) && (
                <div>Episode identification needs a series/season analysis choice. Use the analysis panel above.</div>
              )}
              {visiblePipelineItems.some((item) => item.review_code === 'library_collision') && (
                <div>A matched Jellyfin destination already exists. Each affected card lets you encode/replace after verification or delete only the new pipeline copy.</div>
              )}
              {visiblePipelineItems.some((item) => item.review_code === 'catalogue_candidate_help_available') && (
                <div>The community catalogue has a single-upload candidate for this title, but it has not reached two-installation consensus. RipWeaver kept it as a review hint and did not apply it automatically.</div>
              )}
              {visiblePipelineItems.some((item) => item.review_code === 'placeholder_identification_required') && (
                <>
                  <div>Older placeholder episode labels are not verified matches. Restart them together from their preserved staged rips.</div>
                  <button
                    type="button"
                    className="btn btn-primary text-xs"
                    disabled={controlling}
                    onClick={() => restartAllPlaceholderIdentification(visiblePipelineItems.filter((item) => item.review_code === 'placeholder_identification_required'))}
                  >
                    Restart matching for all placeholder titles
                  </button>
                </>
              )}
              {visiblePipelineItems.some((item) => (item.review_code || '').includes('gemini') || (item.review_code || '').includes('special_feature')) && (
                <>
                  <div>Held identification needs a Gemini retry, a manual name, or an explicit hold choice.</div>
                  {visiblePipelineItems.some((item) => ['gemini_evidence_required', 'gemini_catalog_unavailable'].includes(item.review_code || '')) && (
                    <button type="button" className="btn btn-primary text-xs" disabled={controlling} onClick={runHeldGeminiBatch}>
                      Start local evidence and Gemini for all held bonus titles
                    </button>
                  )}
                </>
              )}
              <button type="button" className="btn btn-secondary text-xs" onClick={() => document.getElementById('pipeline-review-actions')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>
                Show required choices
              </button>
            </div>
          )}
          {visiblePipelineItems.length === 0 ? (
            <div className="text-sm text-[var(--text-muted)]">{showLikelyRemovableOnly ? 'No active items are currently flagged as likely removable.' : attentionOnly ? 'No pipeline errors or review choices need attention.' : queueOnly ? 'No actionable downstream work is waiting. Completed items are available in Recently Finished.' : savedJob?.state === 'queued' ? 'This disc has not produced verified rips yet. Complete the MakeMKV confirmation below; identification will enter this queue automatically after each rip verifies.' : selectedDiscFingerprint ? 'No actionable downstream work belongs to this selected disc. Completed items are available in Recently Finished.' : 'Select a disc to see only its downstream items.'}</div>
          ) : (
            <div id="pipeline-review-actions" className="divide-y divide-[var(--border-color)] scroll-mt-4">
              {visiblePipelineItems.map((item) => (
                <div id={pipelineItemElementId(item.media_id)} tabIndex={-1} key={item.media_id} className="scroll-mt-24 rounded-lg py-3 flex flex-wrap items-center justify-between gap-3 outline-none focus:ring-2 focus:ring-amber-300/70">
                  <div>
                    <div className="font-mono text-sm text-white"><span className="mr-2">{stageIcon[item.state === 'completed' ? 'complete' : item.stage] || '⏸️'}</span>{item.media_id}</div>
                    <div className="text-sm font-semibold text-white">Matched title: {item.display_name || 'Not matched yet'}</div>
                    {item.match_summary && <div className="mt-1 max-w-2xl text-xs text-[var(--text-muted)]">{item.match_summary}</div>}
                    {item.review_code === 'catalogue_candidate_help_available' && item.catalogue_candidate_help && (
                      <div className="mt-2 max-w-2xl rounded border border-amber-400/30 bg-amber-500/10 p-2 text-xs text-amber-100">
                        Community candidate: {item.catalogue_candidate_help.series_name} - S{String(item.catalogue_candidate_help.season).padStart(2, '0')}E{String(item.catalogue_candidate_help.episode).padStart(2, '0')} - {item.catalogue_candidate_help.title}. This has one independent upload and was not applied automatically.
                      </div>
                    )}
                    <div className="text-[11px] text-[var(--text-muted)]">The identifier above is the internal recovery ID.</div>
                    <div className={`text-xs font-semibold ${item.state === 'running' ? 'text-blue-200' : item.state === 'queued' ? 'text-amber-200' : item.state === 'failed' ? 'text-red-200' : 'text-[var(--text-muted)]'}`}>
                      {pipelineStatusLabel(item, pipelineQueue.paused)}
                      {item.review_code ? ` · ${item.review_code}` : ''}
                      {item.error_type ? ` · ${item.error_type}` : ''}
                    </div>
                    {item.state === 'failed' && item.error_type && pipelineErrorHelp[item.error_type] && (
                      <div className="mt-2 max-w-2xl rounded border border-red-400/30 bg-red-500/10 p-2 text-xs text-red-100">
                        {pipelineErrorHelp[item.error_type]}
                      </div>
                    )}
                    {item.visual_review_code && (
                      <div className={`mt-2 inline-flex rounded border px-2 py-1 text-xs font-semibold ${item.likely_removable ? 'border-amber-400/50 bg-amber-500/15 text-amber-100' : 'border-blue-400/30 bg-blue-500/10 text-blue-100'}`}>
                        Visual review: {item.visual_review_code.replaceAll('_', ' ')}{item.likely_removable ? ' · likely removable' : ''}
                      </div>
                    )}
                    {item.possibly_stalled && (
                      <div className="mt-2 max-w-2xl rounded border border-amber-400/40 bg-amber-500/15 p-2 text-xs text-amber-100">
                        Possibly stalled: this {item.stage} stage has not changed for {formatActivityAge(item.activity_age_seconds)}. RipWeaver has not stopped or retried it automatically.
                      </div>
                    )}
                    {(item.identification_attempts?.length || 0) > 0 && (
                      <>
                        <div className="mt-1 max-w-2xl text-[11px] text-[var(--text-muted)]">
                          Identification tried: {item.identification_attempts!.map((attempt) => `${attempt.branch} (${attempt.disposition})`).join(' → ')}
                        </div>
                        {item.identification_attempts!.some((attempt) => matchEvidenceEntries(attempt.summary).length > 0) && (
                          <details className="mt-2 max-w-2xl border-y border-[var(--border-color)] py-2 text-xs">
                            <summary className="cursor-pointer font-semibold text-white">Why this title matched or needs review</summary>
                            <div className="mt-2 divide-y divide-[var(--border-color)]">
                              {item.identification_attempts!.map((attempt, index) => {
                                const evidence = matchEvidenceEntries(attempt.summary);
                                if (evidence.length === 0) return null;
                                return (
                                  <div key={`${attempt.branch}-${index}`} className="py-2">
                                    <div className="font-semibold text-white">{attempt.branch} · {attempt.disposition}</div>
                                    <dl className="mt-1 grid grid-cols-[minmax(8rem,auto)_1fr] gap-x-3 gap-y-1 text-[var(--text-muted)]">
                                      {evidence.map(([key, value]) => (
                                        <Fragment key={key}>
                                          <dt>{evidenceLabels[key]}</dt>
                                          <dd className="break-words text-white">{formatEvidenceValue(key, value)}</dd>
                                        </Fragment>
                                      ))}
                                    </dl>
                                  </div>
                                );
                              })}
                            </div>
                          </details>
                        )}
                      </>
                    )}
                    <IdentificationAuditPanel
                      item={item}
                      audit={item.disc_fingerprint ? identificationAudits[item.disc_fingerprint] ?? null : null}
                      loading={loadingIdentificationAudit === item.disc_fingerprint}
                      error={item.disc_fingerprint ? identificationAuditErrors[item.disc_fingerprint] ?? null : null}
                      onLoad={() => { if (item.disc_fingerprint) void loadIdentificationAudit(item.disc_fingerprint); }}
                    />
                  </div>
                  <div className="flex gap-1 text-xs">
                    {pipelineStages.map((stage) => <span key={stage} className={`rounded border px-2 py-1 transition-colors ${pipelineStageClass(item, stage)}`}>{stageIcon[stage]} {stage}</span>)}
                  </div>
                  {item.state === 'queued' && (
                    <div className="flex flex-wrap gap-2">
                      <button type="button" className="btn btn-secondary text-xs" disabled={controlling} onClick={() => cancelQueuedPipelineItems([item.media_id])}>
                        {item.staged_source_available ? 'Remove from queue — keep staged files' : 'Clear missing staged record'}
                      </button>
                      {item.stage === 'transcode' && item.staged_source_available && (
                        <button type="button" className="btn text-xs border border-red-400/50 bg-red-500/15 text-red-100 hover:bg-red-500/25" disabled={controlling} onClick={() => deleteQueuedStagedSource(item)}>
                          Delete staged rip permanently
                        </button>
                      )}
                    </div>
                  )}
                  {item.stage === 'organize' && item.state === 'queued' && (
                    <div className="max-w-md rounded-lg border border-blue-400/30 bg-blue-400/10 p-3 text-xs text-blue-100">
                      Transcoding and verification finished. Waiting for the organization worker to transfer this file to Jellyfin. An unrelated transcode does not block this move, and no destination collision has been detected.
                    </div>
                  )}
                  {item.stage === 'organize' && item.state === 'completed' && (
                    <div className="max-w-md rounded-lg border border-green-400/30 bg-green-400/10 p-3 text-xs text-green-100">
                      <div>Moved into Jellyfin on {new Date(item.updated_at).toLocaleString()}.</div>
                      {formatBytes(item.output_size_bytes) && <div className="mt-1">Finished file size: {formatBytes(item.output_size_bytes)}</div>}
                      {item.retained_source_available && <div className="mt-1">Original retained in staging for deletion/reprocessing for up to {retainedSourceTtlDays} day(s).</div>}
                      {item.original_source_unavailable && <div className="mt-1">The original staged rip was already unavailable, so there was nothing to archive. The verified encode was still placed safely.</div>}
                      {fullLibraryPath(item) && <div className="mt-2 break-all font-mono text-[11px] text-green-50">{fullLibraryPath(item)}</div>}
                      {item.provisional_match && <div className="mt-3 rounded border border-amber-400/30 bg-amber-400/10 p-3 text-amber-100"><div>Gemini provisional match{item.gemini_confidence !== null ? ` · ${Math.round(item.gemini_confidence * 100)}% confidence` : ''}. Review and rename it if needed.</div><div className="mt-2 flex flex-wrap gap-2"><button type="button" className="btn btn-secondary text-xs" onClick={() => playReview(item.media_id)}>Play for review</button><input className="input-field min-w-64 text-xs" value={renameDrafts[item.media_id] || ''} onChange={(event) => setRenameDrafts((current) => ({ ...current, [item.media_id]: event.target.value }))} placeholder="New filename (without .mkv)" /><button type="button" className="btn btn-primary text-xs" disabled={!renameDrafts[item.media_id]?.trim()} onClick={() => renameProvisional(item.media_id)}>Rename reviewed file</button></div></div>}
                    </div>
                  )}
                  {['failed', 'review_required'].includes(item.state) && (
                    item.stage === 'organize' && item.error_type === 'PipelineQueueError' && item.original_source_unavailable ? (
                      <div className="max-w-md rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-100">
                        <div>The verified encode is preserved, but the old ripped source is no longer in staging. Nothing needs to be archived before Jellyfin placement.</div>
                        <div className="mt-2">After updating or restarting RipWeaver, retry this item to place the verified encode without reripping or retranscoding.</div>
                        <div className="mt-3 flex flex-wrap gap-2">
                          <button type="button" className="btn btn-primary text-xs" disabled={controlling} onClick={() => controlPipeline('resume', item.media_id)}>Retry Jellyfin placement</button>
                          <button type="button" className="btn btn-secondary text-xs" disabled={controlling} onClick={() => dismissPipelineItems([item.media_id])}>Clear from queue and keep encode</button>
                        </div>
                      </div>
                    ) : item.review_code === 'corrected_identification_ready' ? (
                      <div className="max-w-md rounded-lg border border-green-400/30 bg-green-400/10 p-3 text-xs text-green-100">
                        <div>The episode identity was corrected using the independent matches from this disc. The verified encode was preserved and no media was moved or renamed.</div>
                        <div className="mt-2">Confirm below only when you want RipWeaver to place this corrected episode in Jellyfin.</div>
                        <div className="mt-3 flex flex-wrap gap-2">
                          <button
                            type="button"
                            className="btn btn-primary text-xs"
                            disabled={controlling}
                            onClick={() => {
                              if (window.confirm(`Place “${item.display_name || item.media_id}” in Jellyfin using the corrected episode identity? This moves the existing verified encode; it does not rerip or retranscode.`)) void controlPipeline('resume', item.media_id);
                            }}
                          >
                            Place corrected episode in Jellyfin
                          </button>
                          <button type="button" className="btn btn-secondary text-xs" disabled={controlling} onClick={() => dismissPipelineItems([item.media_id])}>Keep encode and clear from queue</button>
                        </div>
                      </div>
                    ) : item.review_code === 'library_collision' ? (
                      <div className="max-w-2xl rounded-lg border border-red-400/30 bg-red-400/10 p-3 text-xs text-red-100">
                        <div>An existing Jellyfin episode conflicts with this matched title. Choose exactly what happens to the new pipeline media.</div>
                        {item.stage === 'organize' && (
                          <div className="mt-3 flex flex-wrap gap-2">
                            <button type="button" className="btn btn-secondary text-xs" disabled={inspectingCollisionId === item.media_id} onClick={() => inspectLibraryCollision(item)}>
                              {inspectingCollisionId === item.media_id ? 'Inspecting file differences…' : 'Inspect file differences'}
                            </button>
                            <button type="button" className="btn btn-secondary text-xs" disabled={openingReviewId === `${item.media_id}:new-encode`} onClick={() => playLibraryCollision(item, 'new-encode')}>
                              {openingReviewId === `${item.media_id}:new-encode` ? 'Opening new encode…' : 'Play new encode'}
                            </button>
                            <button type="button" className="btn btn-secondary text-xs" disabled={openingReviewId === `${item.media_id}:existing-jellyfin`} onClick={() => playLibraryCollision(item, 'existing-jellyfin')}>
                              {openingReviewId === `${item.media_id}:existing-jellyfin` ? 'Opening Jellyfin file…' : 'Play existing Jellyfin file'}
                            </button>
                          </div>
                        )}
                        {collisionComparisonErrors[item.media_id] && <div className="mt-2 text-red-200">Comparison failed safely: {collisionComparisonErrors[item.media_id]}</div>}
                        {collisionComparisons[item.media_id] && (() => {
                          const comparison = collisionComparisons[item.media_id];
                          const rows = [
                            ['Modified', formatCollisionDate(comparison.new_pipeline_file.modified_at), formatCollisionDate(comparison.existing_jellyfin_file.modified_at)],
                            ['Size', formatBytes(comparison.new_pipeline_file.size_bytes), formatBytes(comparison.existing_jellyfin_file.size_bytes)],
                            ['Duration', formatCollisionDuration(comparison.new_pipeline_file.duration_seconds), formatCollisionDuration(comparison.existing_jellyfin_file.duration_seconds)],
                            ['Overall bitrate', formatCollisionBitrate(comparison.new_pipeline_file.overall_bitrate_bps, comparison.new_pipeline_file.overall_bitrate_source), formatCollisionBitrate(comparison.existing_jellyfin_file.overall_bitrate_bps, comparison.existing_jellyfin_file.overall_bitrate_source)],
                            ['Resolution', formatCollisionResolution(comparison.new_pipeline_file), formatCollisionResolution(comparison.existing_jellyfin_file)],
                            ['Frame rate', comparison.new_pipeline_file.frame_rate_fps ? `${comparison.new_pipeline_file.frame_rate_fps.toFixed(3)} fps` : 'Not reported', comparison.existing_jellyfin_file.frame_rate_fps ? `${comparison.existing_jellyfin_file.frame_rate_fps.toFixed(3)} fps` : 'Not reported'],
                            ['Video codec', comparison.new_pipeline_file.video_codec || 'Unknown', comparison.existing_jellyfin_file.video_codec || 'Unknown'],
                            ['Video profile', comparison.new_pipeline_file.video_profile || 'Not reported', comparison.existing_jellyfin_file.video_profile || 'Not reported'],
                            ['Video bitrate', formatCollisionBitrate(comparison.new_pipeline_file.video_bitrate_bps), formatCollisionBitrate(comparison.existing_jellyfin_file.video_bitrate_bps)],
                            ['Pixel format / depth', formatCollisionPixel(comparison.new_pipeline_file), formatCollisionPixel(comparison.existing_jellyfin_file)],
                            ['HDR / color', formatCollisionColor(comparison.new_pipeline_file), formatCollisionColor(comparison.existing_jellyfin_file)],
                            ['Encoder / muxer', formatCollisionEncoder(comparison.new_pipeline_file), formatCollisionEncoder(comparison.existing_jellyfin_file)],
                            ['Audio codec(s)', comparison.new_pipeline_file.audio_codecs.join(', ') || 'Unknown', comparison.existing_jellyfin_file.audio_codecs.join(', ') || 'Unknown'],
                            ...Array.from({ length: Math.max(comparison.new_pipeline_file.audio_tracks.length, comparison.existing_jellyfin_file.audio_tracks.length) }, (_, index) => [
                              `Audio track ${index + 1}`,
                              formatCollisionAudioTrack(comparison.new_pipeline_file, index),
                              formatCollisionAudioTrack(comparison.existing_jellyfin_file, index),
                            ]),
                            ['Container', comparison.new_pipeline_file.container || 'Unknown', comparison.existing_jellyfin_file.container || 'Unknown'],
                          ];
                          return (
                            <div className="mt-3 overflow-x-auto rounded border border-red-300/20 bg-black/20">
                              <table className="w-full min-w-[34rem] text-left">
                                <thead><tr><th className="p-2">Property</th><th className="p-2">New pipeline encode</th><th className="p-2">Existing Jellyfin file</th></tr></thead>
                                <tbody>{rows.map(([property, newValue, existingValue]) => (
                                  <tr key={property} className="border-t border-red-300/15"><th className="p-2 font-medium">{property}</th><td className="p-2">{newValue}</td><td className="p-2">{existingValue}</td></tr>
                                ))}</tbody>
                              </table>
                              <div className="border-t border-red-300/15 p-2 text-red-100">Size difference (new − existing): {comparison.size_difference_bytes >= 0 ? '+' : '−'}{formatBytes(Math.abs(comparison.size_difference_bytes))}</div>
                            </div>
                          );
                        })()}
                        <div className="mt-3 flex flex-wrap gap-2">
                          <button type="button" className="btn btn-primary text-xs" disabled={controlling} onClick={() => resolveLibraryCollision(item, 'replace-library')}>{item.stage === 'organize' ? 'Back up old file and replace it' : 'Encode, then replace after verification'}</button>
                          <button type="button" className="btn btn-secondary text-xs" disabled={controlling} onClick={() => resolveLibraryCollision(item, 'delete-new')}>Delete the new {item.stage === 'organize' ? 'encode' : 'rip'}</button>
                        </div>
                      </div>
                    ) : item.review_code === 'placeholder_identification_required' ? (
                      <div className="max-w-md rounded-lg border border-red-400/30 bg-red-400/10 p-3 text-xs text-red-100">
                        <div>RipWeaver rejected an older placeholder name such as “Unmatched - S01E01 - Episode 1” before HandBrake could start. That label is not a verified match.</div>
                        <div className="mt-3 flex flex-wrap gap-2">
                          <button type="button" className="btn btn-primary text-xs" disabled={controlling} onClick={() => restartPlaceholderIdentification(item)}>Remove placeholder and restart matching</button>
                          <button type="button" className="btn btn-secondary text-xs" disabled={controlling} onClick={() => dismissPipelineItems([item.media_id])}>Leave out of active queue</button>
                          <button type="button" className="btn text-xs border border-red-400/50 bg-red-500/15 text-red-100 hover:bg-red-500/25" disabled={controlling} onClick={() => deleteQueuedStagedSource(item)}>Delete staged rip permanently</button>
                        </div>
                      </div>
                    ) : item.review_code === 'catalogue_candidate_help_available' && item.catalogue_candidate_help ? (
                      <div className="max-w-md rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-100">
                        <div className="font-semibold">One community upload suggests this episode</div>
                        <div className="mt-1">
                          {item.catalogue_candidate_help.series_name} - S{String(item.catalogue_candidate_help.season).padStart(2, '0')}E{String(item.catalogue_candidate_help.episode).padStart(2, '0')} - {item.catalogue_candidate_help.title}
                        </div>
                        <div className="mt-2">It has not reached two-installation consensus. You may use it as server-assisted evidence, enter a different independently reviewed name, or leave the title on hold.</div>
                        <div className="mt-3 flex flex-wrap gap-2">
                          {item.staged_source_available && (
                            <button type="button" className="btn btn-secondary text-xs" disabled={openingReviewId === item.media_id} onClick={() => playReview(item.media_id)}>{openingReviewId === item.media_id ? 'Opening staged rip…' : 'Play staged rip for review'}</button>
                          )}
                          <button
                            type="button"
                            className="btn btn-primary text-xs"
                            disabled={reviewingItemId === item.media_id}
                            onClick={() => saveManualEpisodeIdentification(
                              item,
                              `${item.catalogue_candidate_help!.series_name} - S${String(item.catalogue_candidate_help!.season).padStart(2, '0')}E${String(item.catalogue_candidate_help!.episode).padStart(2, '0')} - ${item.catalogue_candidate_help!.title}`,
                              'catalogue_candidate',
                            )}
                          >
                            Use community candidate and continue
                          </button>
                          <input
                            className="input-field min-w-72 text-xs"
                            value={renameDrafts[item.media_id] || ''}
                            onChange={(event) => setRenameDrafts((current) => ({ ...current, [item.media_id]: event.target.value }))}
                            placeholder="Different reviewed: Series - S03E02 - Episode Title"
                            aria-label="Different independently reviewed episode name"
                          />
                          <button type="button" className="btn btn-secondary text-xs" disabled={reviewingItemId === item.media_id || !renameDrafts[item.media_id]?.trim()} onClick={() => saveManualEpisodeIdentification(item)}>
                            Save different reviewed match
                          </button>
                          <button type="button" className="btn btn-secondary text-xs" disabled={controlling} onClick={() => controlPipeline('resume', item.media_id)}>Retry local identification</button>
                          <button type="button" className="btn btn-secondary text-xs" disabled={controlling} onClick={() => dismissPipelineItems([item.media_id])}>Leave out of active queue</button>
                          <button type="button" className="btn text-xs border border-red-400/50 bg-red-500/15 text-red-100 hover:bg-red-500/25" disabled={controlling} onClick={() => deleteQueuedStagedSource(item)}>Delete staged rip permanently</button>
                        </div>
                      </div>
                    ) : allSeasonAnalysisRunning(item) ? (
                      <div className="max-w-md rounded-lg border border-blue-400/30 bg-blue-400/10 p-3 text-xs text-blue-100">
                        <div className="font-semibold">Exhaustive episode matching is continuing automatically</div>
                        <div className="mt-1">The initial six-window pass was inconclusive. RipWeaver will run the offset transcript windows, whole-season subtitle matching, same-disc residual checks, and the configured final fallback before presenting a human review choice.</div>
                        <div className="mt-2">No action is required. This is an intermediate fallback state, not a completed identification failure.</div>
                      </div>
                    ) : requiresDiscWideTvRecovery(item) ? (
                      <div className="max-w-md rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-100">
                        <div>{item.review_code === 'episode_match_review'
                          ? 'Initial episode matching could not safely approve a title. Review the suggested match or retry the entire disc with exhaustive matching.'
                          : tvEpisodeProviderReviewCodes.has(item.review_code || '')
                            ? 'A later provider or content fallback stopped after TV matching. Retry the complete disc so normal, supplemental, and alternate-release subtitle evidence runs again before Gemini.'
                            : 'This title needs episode-sequence analysis before it can continue.'}</div>
                        {!item.identification_attempts?.length && <div className="mt-2">No matcher attempt is recorded for this preserved title. Run the disc-wide identification retry once; this reads the existing staged MKV and does not rerip the disc.</div>}
                        {item.review_code === 'all_season_catalog_unavailable' && <div className="mt-2">TMDb did not return an aired episode catalogue for the reviewed series name. Open the disc dashboard, confirm the canonical series name, and retry.</div>}
                        {item.review_code === 'all_season_series_not_found' && <div className="mt-2">No TV series could be validated from the supplied name. When automatic Gemini fallback is enabled, inexact labels are sent to Gemini before this review is shown.</div>}
                        {item.review_code === 'all_season_evidence_failed' && <div className="mt-2">Audio evidence collection failed before episode matching began. Review the server diagnostic for the safe failure category, then retry.</div>}
                        {item.review_code === 'whole_disc_coherence_review_required' && <div className="mt-2">RipWeaver withheld every proposed assignment because the complete disc set crossed seasons, repeated an episode, or covered an episode range wider than this disc can plausibly contain.</div>}
                        {['episode_match_review', 'independent_episode_evidence_required', 'whole_disc_coherence_review_required'].includes(item.review_code || '') && episodeReviewCandidates(item).length > 0 && (
                          <div className="mt-3 rounded-lg border border-amber-300/40 bg-black/15 p-3 text-sm">
                            <div className="font-semibold">Suggested episode for human review</div>
                            <div className="mt-1 text-xs text-amber-100/80">Automatic matching exhausted its safe options but did not have enough independent evidence to apply this choice. Open the staged rip and verify the episode before confirming it.</div>
                            <div className="mt-3 flex flex-col items-start gap-2">
                              {episodeReviewCandidates(item).map((candidate) => (
                                <button
                                  key={candidate.name}
                                  type="button"
                                  className="btn btn-primary text-left text-xs"
                                  disabled={!reviewPlaybackOpened.has(item.media_id) || reviewingItemId === item.media_id}
                                  title={reviewPlaybackOpened.has(item.media_id) ? `Confirm ${candidate.name}` : 'Play the staged rip before confirming this episode'}
                                  onClick={() => saveManualEpisodeIdentification(item, candidate.name)}
                                >
                                  Confirm {candidate.name} · {candidate.source}{episodeCandidateDetail(candidate)}
                                </button>
                              ))}
                            </div>
                            <div className="mt-2 text-xs">{reviewPlaybackOpened.has(item.media_id) ? 'Playback opened for review. Confirm the match only after checking the episode.' : 'Confirmation is locked until “Play staged rip for review” opens successfully.'}</div>
                          </div>
                        )}
                        <div className="mt-3 flex flex-wrap gap-2">
                          {item.staged_source_available && (
                            <>
                              <button type="button" className="btn btn-primary text-xs" disabled={openingReviewId === item.media_id} onClick={() => playReview(item.media_id)}>
                                {openingReviewId === item.media_id ? 'Opening staged rip…' : reviewPlaybackOpened.has(item.media_id) ? 'Open staged rip again' : 'Play staged rip for review'}
                              </button>
                              <input
                                className="input-field min-w-72 text-xs"
                                value={renameDrafts[item.media_id] || ''}
                                onChange={(event) => setRenameDrafts((current) => ({ ...current, [item.media_id]: event.target.value }))}
                                placeholder={bonusReviewModes[item.media_id] ? 'Bonus title' : 'Series - S03E02 - Episode Title'}
                                aria-label="Reviewed filename without .mkv"
                              />
                              <label className="flex items-center gap-2 text-xs">
                                <input
                                  type="checkbox"
                                  checked={Boolean(bonusReviewModes[item.media_id])}
                                  onChange={(event) => setBonusReviewModes((current) => ({ ...current, [item.media_id]: event.target.checked }))}
                                />
                                Bonus content
                              </label>
                              <button type="button" className="btn btn-primary text-xs" disabled={reviewingItemId === item.media_id || !renameDrafts[item.media_id]?.trim() || (['episode_match_review', 'independent_episode_evidence_required', 'whole_disc_coherence_review_required'].includes(item.review_code || '') && !reviewPlaybackOpened.has(item.media_id))} onClick={() => saveManualEpisodeIdentification(item)}>
                                Save reviewed name and continue
                              </button>
                            </>
                          )}
                          {!queueOnly && !attentionOnly && selectedDiscFingerprint === item.disc_fingerprint ? (
                            <>
                              <button type="button" className="btn btn-primary text-xs" disabled={controlling} onClick={runAllSeasonAnalysis}>
                                Analyze as TV {suggestedSeason === null ? 'series' : `Season ${suggestedSeason}`}
                              </button>
                              <button type="button" className="btn btn-secondary text-xs" disabled={controlling} onClick={classifyAsMovieExtras}>
                                Analyze as movie bonus feature
                              </button>
                            </>
                          ) : (
                            <>
                              {sequenceRecoveryByDisc.get(item.disc_fingerprint || '')?.firstMediaId === item.media_id && (
                              <button type="button" className="btn btn-primary text-xs" disabled={submittingSeriesRecovery === item.disc_fingerprint} onClick={() => analyzeHeldItemAsTv(item)}>
                                  Retry all {sequenceRecoveryByDisc.get(item.disc_fingerprint || '')?.itemCount} held titles as TV {inferDiscSeason(item.media_id) === null ? 'series' : `Season ${inferDiscSeason(item.media_id)}`}
                                </button>
                              )}
                              <button type="button" className="btn btn-secondary text-xs" onClick={onOpenDashboard}>Open disc controls</button>
                            </>
                          )}
                          <button type="button" className="btn btn-secondary text-xs" disabled={controlling} onClick={() => dismissPipelineItems([item.media_id])}>Leave out of active queue</button>
                          <button type="button" className="btn text-xs border border-red-400/50 bg-red-500/15 text-red-100 hover:bg-red-500/25" disabled={controlling} onClick={() => deleteQueuedStagedSource(item)}>Delete staged rip permanently</button>
                        </div>
                      </div>
                    ) : item.review_code === 'visual_content_review_required' ? (
                      <div className="max-w-md rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-100">
                        <div>Local frame OCR classified this title as a likely warning screen or disc menu. It has been excluded from episode matching and preserved for your review.</div>
                        <div className="mt-2">RipWeaver will never delete it automatically.</div>
                        <div className="mt-3 flex flex-wrap gap-2">
                          {item.staged_source_available && <button type="button" className="btn btn-primary text-xs" disabled={openingReviewId === item.media_id} onClick={() => playReview(item.media_id)}>{openingReviewId === item.media_id ? 'Opening staged rip…' : 'Play staged rip for review'}</button>}
                          <button type="button" className="btn btn-secondary text-xs" disabled={controlling} onClick={() => dismissPipelineItems([item.media_id])}>Leave out of active queue</button>
                          <button type="button" className="btn text-xs border border-red-400/50 bg-red-500/15 text-red-100 hover:bg-red-500/25" disabled={controlling} onClick={() => deleteQueuedStagedSource(item)}>Delete staged rip permanently</button>
                        </div>
                      </div>
                    ) : item.review_code === 'play_all_aggregate_detected' ? (
                      <div className="mt-2">This unmatched file closely matches the combined runtime and size of already matched contiguous episodes. It is being preserved as a likely play-all aggregate and is excluded from the missing-episode count.</div>
                    ) : ['special_feature_evidence_required', 'gemini_evidence_required', 'gemini_analysis_running', 'gemini_analysis_interrupted', 'gemini_analysis_failed', 'gemini_audio_evidence_insufficient', 'gemini_catalog_unavailable', 'gemini_provider_failed', 'gemini_credential_rejected', 'gemini_rate_limited', 'gemini_provider_unavailable', 'gemini_request_rejected', 'gemini_network_failed', 'gemini_response_invalid', 'gemini_series_resolution_uncertain', 'gemini_descriptive_review_required', 'special_feature_manual_assignment_required'].includes(item.review_code || '') ? (
                      <div className="max-w-md rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-100">
                        <div>This title still needs a confident identification. It remains held; unrelated titles can continue through the queue.</div>
                        {item.review_code === 'gemini_evidence_required' && <div className="mt-2">Gemini fallback selected. Local evidence must be prepared first; selecting this did not contact Gemini or start HandBrake.</div>}
                        {item.review_code === 'gemini_analysis_running' && <div className="mt-2">Collecting bounded local audio evidence and running the confirmed Gemini comparison. It will requeue identification automatically when a confident allowed match is returned.</div>}
                        {item.review_code === 'gemini_analysis_interrupted' && <div className="mt-2">The previous RipWeaver process stopped while Gemini analysis was marked active. No provider task is running; retry to start a new traced attempt.</div>}
                        {item.review_code === 'gemini_analysis_failed' && <div className="mt-2">The evidence or Gemini request failed safely. No title was guessed; you may retry or choose a name manually.</div>}
                        {item.review_code === 'gemini_audio_evidence_insufficient' && <div className="mt-2">Local transcription did not produce enough usable dialogue. No Gemini request or title guess was made.</div>}
                        {item.review_code === 'gemini_catalog_unavailable' && <div className="mt-2">No reviewed bonus-feature catalogue matched this disc. Retry now uses bounded local evidence and catalogue-free Gemini classification to propose a provisional movie or extra name.</div>}
                        {item.review_code === 'gemini_descriptive_review_required' && (
                          <div className="mt-2">
                            {item.identification_attempts?.some((attempt) => attempt.branch === 'tv-movie')
                              ? 'RipWeaver checked feature-length movies related to this TV series before bonus-feature naming, but no movie had sufficiently distinct subtitle evidence.'
                              : item.identification_attempts?.some((attempt) => attempt.branch === 'tv-bonus')
                                ? 'Gemini reviewed this as possible TV-disc bonus content but could not assign one safe descriptive name. Episode and related-movie matching were attempted first.'
                              : 'Gemini reviewed the bounded evidence but could not assign one safe movie or bonus-feature name.'}
                          </div>
                        )}
                        {item.review_code === 'gemini_provider_failed' && <div className="mt-2">The Gemini provider request failed safely. Check its credential status and network availability before retrying.</div>}
                        {item.review_code === 'gemini_credential_rejected' && <div className="mt-2">Gemini rejected or could not use the configured credentials. Check the key identifiers in Settings and rotate the rejected key.</div>}
                        {item.review_code === 'gemini_rate_limited' && <div className="mt-2">Gemini returned a quota or rate-limit response after bounded retries. Wait for quota recovery or check billing.</div>}
                        {item.review_code === 'gemini_provider_unavailable' && <div className="mt-2">Gemini returned a server error after bounded retries. Retry after the provider recovers.</div>}
                        {item.review_code === 'gemini_request_rejected' && <div className="mt-2">Gemini rejected the model/request combination. Check model availability and request compatibility.</div>}
                        {item.review_code === 'gemini_network_failed' && <div className="mt-2">RipWeaver could not reach Gemini after bounded retries. Check DNS, firewall, proxy, and internet access.</div>}
                        {item.review_code === 'gemini_response_invalid' && (
                          <div className="mt-2">
                            {item.identification_attempts?.some((attempt) => ['tv-movie', 'tv-bonus', 'movie-bonus'].includes(attempt.branch))
                              ? 'Gemini responded, but its structured movie or bonus-feature result did not pass validation.'
                              : 'Gemini responded, but its structured episode assignments did not pass validation.'}
                          </div>
                        )}
                        {item.review_code === 'gemini_series_resolution_uncertain' && (
                          <div className="mt-2 space-y-2">
                            <div>
                              {item.gemini_series_proposal
                                ? `Gemini’s ranked suggestions were ${item.gemini_series_proposal.series_names.map((name, index) => `${index + 1}. “${name}”`).join(', ')}. Its first choice was ${Math.round(item.gemini_series_proposal.confidence * 100)}% confidence${item.gemini_series_proposal.tmdb_id ? ` (TMDb ${item.gemini_series_proposal.tmdb_id})` : ''}, but RipWeaver could not validate any identity confidently through TMDb.`
                                : 'Gemini already reviewed the unusual disc label, but its proposed series could not be validated confidently through TMDb. This older attempt did not retain the proposal.'}
                              {' '}This is the first point where a canonical series name is required.
                            </div>
                            {seriesResolutionRecoveryByDisc.get(item.disc_fingerprint || item.media_id) === item.media_id ? (
                              <div className="rounded border-2 border-red-400 bg-red-950/20 p-3">
                                <label className="mb-2 block text-sm font-semibold text-red-100" htmlFor={`series-recovery-${item.media_id}`}>
                                  Tell RipWeaver the TV series name
                                </label>
                                <div className="flex flex-wrap gap-2">
                                  <input
                                    id={`series-recovery-${item.media_id}`}
                                    className="min-w-72 rounded border-2 border-red-500 bg-white px-3 py-2 text-sm font-semibold text-slate-950 shadow-inner outline-none placeholder:text-red-600 placeholder:opacity-100 focus:border-red-300 focus:ring-2 focus:ring-red-300"
                                    value={seriesDrafts[item.disc_fingerprint || item.media_id] || ''}
                                    onChange={(event) => setSeriesDrafts((current) => ({ ...current, [item.disc_fingerprint || item.media_id]: event.target.value }))}
                                    placeholder="Type TV series name here"
                                    aria-label="Type TV series name here"
                                  />
                                  <button
                                    type="button"
                                    className="btn btn-primary text-xs"
                                    disabled={Boolean(submittingSeriesRecovery) || !seriesDrafts[item.disc_fingerprint || item.media_id]?.trim()}
                                    onClick={() => analyzeHeldItemAsTv(item, seriesDrafts[item.disc_fingerprint || item.media_id])}
                                  >
                                    {submittingSeriesRecovery === item.disc_fingerprint
                                      ? 'Starting this disc analysis…'
                                      : submittingSeriesRecovery
                                        ? 'Waiting for another disc request…'
                                        : 'Use this name and retry the entire disc'}
                                  </button>
                                </div>
                              </div>
                            ) : (
                              <div>The whole-disc canonical-series field is shown once on the first held title from this disc.</div>
                            )}
                          </div>
                        )}
                        {item.review_code === 'gemini_analysis_running' && geminiProgress[item.media_id] && <div className="mt-2 rounded border border-blue-400/30 bg-blue-400/10 p-2 text-blue-100">{geminiProgress[item.media_id]}</div>}
                        {item.review_code === 'special_feature_manual_assignment_required' && <div className="mt-2">Manual feature-name assignment selected.</div>}
                        {episodeReviewCandidates(item).length > 0 && (
                          <div className="mt-3 rounded-lg border border-amber-400/35 bg-amber-500/10 p-3 text-sm text-amber-100">
                            <div className="font-semibold">Candidate episode matches for human review</div>
                            <div className="mt-1 text-xs">Automatic matching exhausted its safe paths without a confident assignment. Open the staged rip before confirming any displayed candidate.</div>
                            <div className="mt-2 flex flex-wrap gap-2">
                              {episodeReviewCandidates(item).map((candidate) => (
                                <button key={candidate.name} type="button" className="btn btn-secondary text-xs" disabled={!reviewPlaybackOpened.has(item.media_id) || reviewingItemId === item.media_id} title={reviewPlaybackOpened.has(item.media_id) ? `Confirm ${candidate.name}` : 'Play the staged rip before confirming this episode'} onClick={() => saveManualEpisodeIdentification(item, candidate.name)}>
                                  Confirm {candidate.name} · {candidate.source}{episodeCandidateDetail(candidate)}
                                </button>
                              ))}
                            </div>
                            <div className="mt-2 text-xs">{reviewPlaybackOpened.has(item.media_id) ? 'Playback opened for review. Confirm only after checking the episode.' : 'Candidate confirmation is locked until playback opens successfully.'}</div>
                          </div>
                        )}
                        {item.review_code === 'gemini_descriptive_review_required' && (
                          <div className="mt-3 rounded-lg border border-indigo-400/35 bg-indigo-500/10 p-3 text-sm text-indigo-100">
                            <label className="font-semibold" htmlFor={`scene-description-${item.media_id}`}>Tell Gemini what happens in the scenes you recognize</label>
                            <textarea
                              id={`scene-description-${item.media_id}`}
                              className="input-field mt-2 min-h-24 w-full text-xs"
                              maxLength={1200}
                              value={sceneDescriptionDrafts[item.media_id] || ''}
                              onChange={(event) => setSceneDescriptionDrafts((current) => ({ ...current, [item.media_id]: event.target.value }))}
                              placeholder="Example: Michael burns his foot on a George Foreman grill, then Dwight crashes his car while rushing to help him."
                            />
                            <div className="mt-2 flex flex-wrap items-center gap-2">
                              <button
                                type="button"
                                className="btn btn-primary text-xs"
                                disabled={Boolean(submittingSceneReviewId) || (sceneDescriptionDrafts[item.media_id]?.trim().length || 0) < 3}
                                onClick={() => analyzeWithSceneDescription(item)}
                              >
                                {submittingSceneReviewId === item.media_id ? 'Starting scene-guided review...' : submittingSceneReviewId ? 'Waiting for another scene review...' : 'Ask Gemini using these scene notes'}
                              </button>
                              <span className="text-xs text-indigo-200">Your note is sent only after confirmation and is not written to the identification history.</span>
                            </div>
                          </div>
                        )}
                        <div className="mt-3 flex flex-wrap gap-2">
                          {item.staged_source_available && (
                            <button type="button" className="btn btn-primary text-xs" disabled={openingReviewId === item.media_id} onClick={() => playReview(item.media_id)}>
                              {openingReviewId === item.media_id ? 'Opening staged rip…' : 'Play staged rip for review'}
                            </button>
                          )}
                          {item.review_code === 'gemini_descriptive_review_required' && (
                            <button type="button" className="btn btn-primary text-xs" disabled={submittingSeriesRecovery === item.disc_fingerprint} onClick={() => analyzeHeldItemAsTv(item)}>Analyze as TV series</button>
                          )}
                          <button type="button" className="btn btn-secondary text-xs" disabled={reviewingItemId === item.media_id || item.review_code === 'gemini_analysis_running'} onClick={() => chooseAmbiguityResolution(item.media_id, 'gemini')}>{item.review_code === 'gemini_descriptive_review_required' ? 'Retry bonus-feature analysis' : item.review_code?.includes('failed') || item.review_code?.startsWith('gemini_') ? 'Retry local evidence and Gemini' : 'Use Gemini after local evidence'}</button>
                          <button type="button" className="btn btn-secondary text-xs" disabled={reviewingItemId === item.media_id} onClick={() => chooseAmbiguityResolution(item.media_id, 'manual')}>Choose name manually</button>
                          <button type="button" className="btn btn-secondary text-xs" disabled={reviewingItemId === item.media_id} onClick={() => chooseAmbiguityResolution(item.media_id, 'hold')}>Leave on hold</button>
                          <button type="button" className="btn btn-secondary text-xs" disabled={controlling} onClick={() => dismissPipelineItems([item.media_id])}>Remove from queue — keep staged rip</button>
                          {item.staged_source_available ? (
                            <button type="button" className="btn text-xs border border-red-400/50 bg-red-500/15 text-red-100 hover:bg-red-500/25" disabled={controlling || item.review_code === 'gemini_analysis_running'} onClick={() => deleteQueuedStagedSource(item)}>Delete staged rip permanently</button>
                          ) : (
                            <button type="button" className="btn btn-secondary text-xs" disabled={controlling || item.review_code === 'gemini_analysis_running'} onClick={() => dismissPipelineItems([item.media_id])}>Clear missing staged record</button>
                          )}
                        </div>
                        {automaticGeminiFallback && <div className="mt-2 text-indigo-100">Automatic final Gemini fallback is enabled in Settings. External use will still be shown in this queue.</div>}
                      </div>
                    ) : (
                      <div className="flex flex-wrap gap-2">
                        {item.staged_source_available && (
                          <button type="button" className="btn btn-primary text-xs" disabled={openingReviewId === item.media_id} onClick={() => playReview(item.media_id)}>
                            {openingReviewId === item.media_id ? 'Opening staged rip…' : 'Play staged rip for review'}
                          </button>
                        )}
                        {item.staged_source_available && item.error_type === 'HandBrakeNoUsableAudio' && (
                          <button
                            type="button"
                            className="btn btn-secondary inline-flex min-w-56 items-center justify-center gap-2 text-xs"
                            disabled={silentVideoOcrRunningId === item.media_id}
                            aria-busy={silentVideoOcrRunningId === item.media_id}
                            onClick={() => analyzeSilentVideo(item)}
                          >
                            {silentVideoOcrRunningId === item.media_id && (
                              <span className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent" aria-hidden="true" />
                            )}
                            {silentVideoOcrRunningId === item.media_id ? 'Analyzing video text...' : 'Analyze silent video text (OCR)'}
                          </button>
                        )}
                        <button type="button" className="btn btn-secondary" onClick={() => controlPipeline('resume', item.media_id)}>
                          Retry item
                        </button>
                        <button type="button" className="btn btn-secondary" disabled={controlling} onClick={() => dismissPipelineItems([item.media_id])}>
                          Clear from queue
                        </button>
                        <button type="button" className="btn text-xs border border-red-400/50 bg-red-500/15 text-red-100 hover:bg-red-500/25" disabled={controlling} onClick={() => deleteQueuedStagedSource(item)}>
                          Delete staged rip permanently
                        </button>
                        {silentVideoOcrRunningId === item.media_id && (
                          <div className="w-full max-w-2xl rounded border border-blue-400/30 bg-blue-500/10 p-3 text-xs text-blue-100" role="status" aria-live="polite">
                            Extracting six bounded frames and analyzing their text. This can take a moment.
                          </div>
                        )}
                        {silentVideoOcr[item.media_id] && (
                          <div className={`w-full max-w-2xl rounded border p-4 text-xs shadow-lg ${silentVideoOcr[item.media_id].category === 'likely_warning_screen' ? 'border-amber-300/70 bg-amber-500/20 text-amber-50 shadow-amber-950/30 ring-1 ring-amber-300/30' : 'border-blue-400/30 bg-blue-500/10 text-blue-100 shadow-black/20'}`}>
                            {silentVideoOcr[item.media_id].category === 'likely_warning_screen' ? (
                              <div className="flex flex-wrap items-center gap-3">
                                <span className="relative flex h-3 w-3" aria-hidden="true">
                                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-300 opacity-60" />
                                  <span className="relative inline-flex h-3 w-3 rounded-full bg-amber-300" />
                                </span>
                                <div className="text-base font-bold text-amber-50">OCR review: likely warning screen</div>
                                <span className="rounded border border-amber-200/60 bg-amber-200/15 px-2 py-1 font-bold text-amber-100">Likely removable</span>
                              </div>
                            ) : (
                              <div className="font-semibold">OCR review: {silentVideoOcr[item.media_id].category.replaceAll('_', ' ')}</div>
                            )}
                            <div className="mt-1">{silentVideoOcr[item.media_id].summary}</div>
                            <div className="mt-2 text-[var(--text-muted)]">Sampled {silentVideoOcr[item.media_id].sampled_frame_count} frames · {silentVideoOcr[item.media_id].ocr_text_characters} OCR character(s)</div>
                            {silentVideoOcr[item.media_id].ocr_excerpt && <div className="mt-2 rounded bg-black/20 p-2 font-mono text-[11px] text-white">{silentVideoOcr[item.media_id].ocr_excerpt}</div>}
                          </div>
                        )}
                        {silentVideoOcrErrors[item.media_id] && (
                          <div className="w-full max-w-2xl rounded border border-red-400/30 bg-red-500/10 p-3 text-xs text-red-100">
                            OCR did not run: {silentVideoOcrErrors[item.media_id]}
                          </div>
                        )}
                      </div>
                    )
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {!queueOnly && !attentionOnly && preview && (
        <div id="selected-disc-review" className="space-y-5 animate-fade-in scroll-mt-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="text-xs uppercase tracking-wider text-[var(--text-muted)]">Selected disc review</div>
              <div className="text-xl font-bold text-white">Reviewing optical drive {(selectedDriveSlot?.drive_index ?? preview.drives[0]?.drive_index ?? 0) + 1}</div>
            </div>
            <button type="button" className="btn btn-secondary" onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}>Back to drives</button>
          </div>
          <div className={`rounded-xl border p-4 ${allReviewedTitlesAlreadyInLibrary
            ? 'border-green-500/30 bg-green-500/10'
            : preview.requires_review
            ? 'border-amber-500/30 bg-amber-500/10'
            : 'border-green-500/30 bg-green-500/10'
          }`}>
            <div className="font-bold text-white">
              {existingRipsNeedAnalysis ? 'Existing rips need episode analysis' : existingRipsInPipeline ? 'Existing rips are processing' : allReviewedTitlesAlreadyInLibrary ? 'Disc already complete in Jellyfin' : catalogueSupportRequired ? 'Automatic catalogue lookup paused' : preview.requires_review ? 'Your attention is needed' : 'Ready for your approval'}
            </div>
            <div className="text-sm text-[var(--text-muted)] mt-1">
              {existingRipsNeedAnalysis
                ? `${preview.jobs.length} completed MKV(s) were accepted, but ordinary identification could not determine their episodes. Start the all-season matcher below.`
                : existingRipsInPipeline
                ? `${preview.jobs.length} existing completed MKV(s) were accepted as inputs. No rerip or overwrite decision is needed while identification proceeds.`
                : allReviewedTitlesAlreadyInLibrary
                ? `No missing titles were found. All ${heldLibraryTitleIndexes.size} known episode destination(s) currently exist in Jellyfin, including resolution-suffixed versions. Nothing will be queued for MakeMKV unless you explicitly choose a rerip.`
                : separatedHeldLibraryTitles.length > 0
                ? `${separatedHeldLibraryTitles.length} known Jellyfin destination(s) were held out of this missing-only plan. ${preview.jobs.length} missing or unresolved title(s) remain for review.`
                : catalogueSupportRequired
                ? 'The automatic monthly allowance and saved credits are exhausted. Nothing was charged. Choose support, contribute a reviewed disc, or continue this lookup manually.'
                : preview.requires_review
                ? `${preview.jobs.length} titles were found, but ${preview.collision_count} existing item(s) must be handled safely before ripping.`
                : `${preview.jobs.length} titles were found and no existing files will be overwritten. Review the selection below, then approve it to continue.`}
            </div>
            {preview.collision_count > 0 && preview.jobs.every((item) => ['clear', 'staging-exists'].includes(item.collision_status)) && (
              <div className="mt-3 rounded-lg border border-amber-500/30 bg-black/10 p-3 text-sm text-amber-100">
                A previous isolated staging attempt already exists. It will not be overwritten or deleted. Use “Prepare fresh isolated attempt” on this drive to create a new collision-safe attempt.
              </div>
            )}
            <div className="flex flex-wrap items-center justify-between gap-3 mt-4">
              <div className="text-xs text-[var(--text-muted)]">{savedJob ? 'Your progress was saved automatically. You can safely close and return to this page.' : 'Save this selection so it can be resumed later.'}</div>
              {!savedJob && (
              <button
                type="button"
                className="btn btn-secondary"
                onClick={saveDurableJob}
                disabled={saving}
              >
                {saving ? 'Saving…' : 'Save selection'}
              </button>
              )}
            </div>
            <details className="mt-3 text-xs text-[var(--text-muted)]">
              <summary className="cursor-pointer">Technical details</summary>
              <div className="font-mono mt-2">Plan fingerprint: {preview.plan_sha256}</div>
              {savedJob && <div className="font-mono mt-1">Recovery ID: {savedJob.job_id}</div>}
            </details>
          </div>

          {catalogueSupportRequired && (
            <div className="rounded-xl border border-violet-400/40 bg-violet-500/10 p-5 space-y-4">
              <div>
                <div className="text-lg font-bold text-violet-50">Automatic catalogue allowance used</div>
                <div className="mt-1 text-sm text-violet-100/80">
                  RipWeaver paused automatic identification before retrieving this disc. You can support the shared catalogue, earn a credit when a reviewed disc contribution is accepted, or continue this lookup manually without paying.
                </div>
              </div>
              {catalogueSupportStatus?.usage && (
                <div className="grid gap-2 text-sm sm:grid-cols-3">
                  <div className="rounded-lg bg-black/15 p-3"><div className="text-violet-100/65">Monthly</div><div className="font-bold text-white">{catalogueSupportStatus.usage.monthly_remaining} remaining</div></div>
                  <div className="rounded-lg bg-black/15 p-3"><div className="text-violet-100/65">Contributed</div><div className="font-bold text-white">{catalogueSupportStatus.usage.contribution_credits} credits</div></div>
                  <div className="rounded-lg bg-black/15 p-3"><div className="text-violet-100/65">Supported</div><div className="font-bold text-white">{catalogueSupportStatus.usage.purchased_credits} credits</div></div>
                </div>
              )}
              <div className="grid gap-4 lg:grid-cols-2">
                <label className="space-y-2 text-sm text-violet-50">
                  <span className="font-semibold">Support amount — minimum ${((supportPolicy?.minimum_amount_cents ?? 1000) / 100).toFixed(2)}</span>
                  <input
                    type="number"
                    min={(supportPolicy?.minimum_amount_cents ?? 1000) / 100}
                    max="1000"
                    step="1"
                    value={(supportAmountCents / 100).toFixed(0)}
                    onChange={(event) => setSupportAmountCents(Math.max(0, Math.round(Number(event.target.value) * 100)))}
                    className="w-full rounded-lg border border-violet-300/25 bg-[var(--bg-primary)] px-3 py-2 text-white"
                  />
                </label>
                <label className="space-y-2 text-sm text-violet-50">
                  <span className="font-semibold">Choose support per automatic lookup: ${(supportRateCents / 100).toFixed(2)}</span>
                  <input
                    type="range"
                    min={supportPolicy?.minimum_rate_cents ?? 1}
                    max={supportPolicy?.maximum_rate_cents ?? 100}
                    step="1"
                    value={supportRateCents}
                    onChange={(event) => setSupportRateCents(Number(event.target.value))}
                    className="w-full"
                  />
                  <span className="block text-xs text-violet-100/65">$0.01 gives the most lookups; $1.00 voluntarily gives RipWeaver more support per lookup.</span>
                </label>
              </div>
              <div className="rounded-lg border border-violet-300/20 bg-black/15 p-3 text-sm text-violet-50">
                ${ (supportAmountCents / 100).toFixed(2) } at ${(supportRateCents / 100).toFixed(2)} per lookup provides <span className="font-bold">{supportLookupCredits.toLocaleString()} automatic lookup credits</span>.
              </div>
              <div className="rounded-lg border border-amber-300/25 bg-amber-500/10 p-3 text-xs text-amber-50 space-y-2">
                <p>{supportPolicy?.availability_disclosure ?? 'RipWeaver is independently operated on a best-effort, as-is, and as-available basis. A support payment does not guarantee future maintenance, technical support, uptime, data preservation, or continued availability. Backups are maintained, but outages, data loss, or permanent discontinuation remain possible. Unused credits may become unusable if the service ends and have no cash value.'}</p>
                <p>{supportPolicy?.refund_disclosure ?? 'Payments are generally final once credits are issued, except where required by law or payment-network rules, or for duplicate, unauthorized, or incorrectly fulfilled payments.'}</p>
                <label className="flex items-start gap-2 font-semibold">
                  <input type="checkbox" className="mt-0.5" checked={supportTermsAccepted} onChange={(event) => setSupportTermsAccepted(event.target.checked)} />
                  <span>I understand that this is a best-effort service with no guarantee of future availability or support.</span>
                </label>
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={startingSupportCheckout || !supportTermsAccepted || supportAmountCents < (supportPolicy?.minimum_amount_cents ?? 1000) || !supportPolicy?.payments_enabled}
                  onClick={beginSupportCheckout}
                >
                  {supportPolicy?.payments_enabled ? `Support $${(supportAmountCents / 100).toFixed(2)} for ${supportLookupCredits.toLocaleString()} lookups` : 'Support checkout is not configured yet'}
                </button>
                <button type="button" className="btn btn-secondary" disabled={preparingDrive !== null} onClick={continueCatalogueLookupManually}>
                  Continue this lookup manually
                </button>
              </div>
              <div className="text-xs text-violet-100/65">Support payments are not represented as charitable or tax-deductible donations. Manual continuation always remains available.</div>
            </div>
          )}

          {savedJob && (
            <div className="glass-panel rounded-xl p-5">
              {reviewNotice && (
                <div className="mb-4 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-100">
                  {reviewNotice}
                </div>
              )}
              {existingRecoveryPlan && !allReviewedTitlesAlreadyInLibrary && (
                <div className="mb-4 rounded-xl border border-blue-500/30 bg-blue-500/10 p-4 space-y-3">
                  <div className="font-semibold text-blue-100">
                    {existingRecoveryPlan.candidates.length > 0 ? 'Exact reusable staged files found' : 'No exact reusable staged files found'}
                  </div>
                  <div className="text-sm text-blue-100/75">
                    {existingRecoveryPlan.candidates.length > 0
                      ? 'These filenames carry this disc fingerprint and title index. Check the files to verify and reuse.'
                      : 'No staged filename or durable verification record safely ties an existing MKV to these title indexes. Older files that were renamed without retaining that identity cannot be guessed automatically.'}
                  </div>
                  <div className="space-y-2">
                    {existingRecoveryPlan.candidates.map((candidate) => (
                      <label key={candidate.candidate_id} className="flex items-center gap-3 text-sm text-white">
                        <input
                          type="radio"
                          name={`recovery-title-${candidate.title_index}`}
                          checked={selectedRecoveryCandidates[candidate.title_index] === candidate.candidate_id}
                          onChange={() => setSelectedRecoveryCandidates((current) => ({ ...current, [candidate.title_index]: candidate.candidate_id }))}
                        />
                        <span>Title {candidate.title_index} · {candidate.basename} · {formatBytes(candidate.size_bytes)}</span>
                      </label>
                    ))}
                  </div>
                  {(existingRecoveryPlan.missing_title_indexes.length > 0 || existingRecoveryPlan.ambiguous_title_indexes.length > 0) && (
                    <div className="text-xs text-amber-200">
                      Missing: {existingRecoveryPlan.missing_title_indexes.join(', ') || 'none'} · Ambiguous duplicates: {existingRecoveryPlan.ambiguous_title_indexes.join(', ') || 'none'}
                    </div>
                  )}
                  {existingRecoveryPlan.candidates.length > 0 ? (
                    <button type="button" className="btn btn-primary" disabled={controlling || Object.keys(selectedRecoveryCandidates).length === 0} onClick={verifyExistingCandidates}>
                      Verify completed titles and continue to missing-title rip
                    </button>
                  ) : (
                    <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-100">
                      To continue safely, start a fresh collision-safe rip below. You can use title inspection to limit that rip to selected titles. Legacy renamed files need a separate explicit file-to-title mapping; FFprobe alone cannot recover a lost MakeMKV title index.
                    </div>
                  )}
                </div>
              )}
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <div className="font-bold text-white">What would you like to do?</div>
                  <div className="text-sm text-[var(--text-muted)] mt-1">Verified files are always preserved here. Incomplete failed attempts are handled by the explicit choice shown before MakeMKV starts.</div>
                </div>
                <span className="px-3 py-1 rounded-full bg-blue-500/15 text-blue-300 text-xs font-bold uppercase">
                  {savedJob.state.replaceAll('_', ' ')}
                </span>
              </div>
              <div className="mt-4 space-y-3">
                {existingRipsInPipeline && (
                  <div className="rounded-xl border border-blue-500/30 bg-blue-500/10 p-4 space-y-3">
                    <div className="font-semibold text-blue-100">Using recovered completed MKVs</div>
                    <div className="text-sm text-blue-100/75 mt-1">
                      {existingRipsNeedAnalysis
                        ? 'Ordinary identification held these files for cross-season analysis. The original MKVs remain preserved.'
                        : 'They have entered identification and will continue automatically. The original files remain preserved. Follow their progress in the selected-disc queue above or the global Queue page.'}
                    </div>
                    {existingRipsNeedAnalysis ? (
                      <div className="flex flex-wrap gap-2">
                        <button type="button" className="btn btn-primary" disabled={controlling} onClick={runAllSeasonAnalysis}>
                          Analyze existing MKVs and continue matching
                        </button>
                        {selectedDriveSlot && (
                          <button
                            type="button"
                            className="btn btn-secondary"
                            disabled={preparingDrive === selectedDriveSlot.drive_index || queuedPrepareDrives.includes(selectedDriveSlot.drive_index)}
                            onClick={() => queueDrivePipeline(selectedDriveSlot, getDiscSetup(driveSetupKey(selectedDriveSlot)))}
                          >
                            Prepare a fresh rerip plan instead
                          </button>
                        )}
                      </div>
                    ) : (
                      <button type="button" className="btn btn-primary" disabled={controlling} onClick={() => controlPipeline('resume')}>
                        Start / resume matching queue
                      </button>
                    )}
                  </div>
                )}
                {savedJob.state === 'awaiting_review' && !preview.requires_review && (
                  <div className="rounded-xl border border-green-500/30 bg-green-500/10 p-4">
                    <div className="font-semibold text-green-100">Continue with these {preview.jobs.length} titles</div>
                    <div className="text-sm text-green-100/70 mt-1">
                      This saves the exact title list and adds the disc to the waiting rip queue. It does not start MakeMKV, overwrite files, remove an earlier attempt, or eject the disc. You will see one final confirmation before ripping starts.
                    </div>
                    <div className="flex flex-wrap gap-2 mt-3">
                      <button className="btn btn-primary" disabled={controlling} onClick={approveAndQueueJob}>
                        Save these {preview.jobs.length} titles and add to rip queue
                      </button>
                      <button type="button" className="btn btn-secondary" disabled={controlling} onClick={existingRipsInPipeline ? approveAndQueueJob : restartExistingPipeline}>
                        {existingRipsInPipeline ? `Resume failed rip with these ${preview.jobs.length} missing titles` : 'Recover completed files and continue missing-title rip'}
                      </button>
                      <button type="button" className="btn btn-secondary" onClick={() => {
                        setSelectingTitles(true);
                        setSelectedTitleIndexes(preview.jobs.map((item) => item.title_index));
                        document.getElementById('planned-title-list')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
                      }}>
                        Inspect the {preview.jobs.length} selected titles
                      </button>
                      <button type="button" className="btn btn-secondary" onClick={keepReviewOnHold}>
                        Keep this disc on hold
                      </button>
                      {selectedDriveSlot && (
                        <button
                          type="button"
                          className="btn btn-secondary"
                          disabled={controlling || preparingDrive !== null}
                          onClick={() => {
                            queueDrivePipeline(selectedDriveSlot, getDiscSetup(driveSetupKey(selectedDriveSlot)));
                          }}
                        >
                          Read disc again and create a fresh review
                        </button>
                      )}
                    </div>
                  </div>
                )}
                {savedJob.state === 'awaiting_review' && preview.requires_review && !existingRipsInPipeline && (
                  <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
                    <div className="font-semibold text-amber-100">Choose how to handle earlier rip output</div>
                    <div className="text-sm text-amber-100/75 mt-1">
                      Existing completed files and partial attempts are never silently overwritten. Choose what should be included in a new immutable review.
                    </div>
                    <div className="grid gap-3 mt-3 md:grid-cols-2 xl:grid-cols-4">
                      <button type="button" className="btn btn-primary text-left" disabled={controlling} onClick={restartExistingPipeline}>
                        {recoverableExistingTitleCount > 0
                          ? `Review ${recoverableExistingTitleCount} preserved title${recoverableExistingTitleCount === 1 ? '' : 's'} for reuse`
                          : 'Reuse verified titles or resume a failed rip'}
                      </button>
                      <button type="button" className="btn btn-primary text-left" disabled={controlling || missingLibraryTitleCount === 0} onClick={() => resolveRipCollisions('missing-only')}>
                        {missingLibraryTitleCount === 0 ? 'No missing titles to rip' : 'Rip only missing titles'}
                      </button>
                      <button type="button" className="btn btn-secondary text-left" disabled={controlling} onClick={() => resolveRipCollisions('rerip-all')}>
                        Rip all titles again as replacement copies
                      </button>
                      <button type="button" className="btn btn-secondary text-left border-red-500/50 text-red-100" disabled={controlling} onClick={() => resolveRipCollisions('replace-after-verification')}>
                        Deliberately replace existing completed files
                      </button>
                    </div>
                    <div className="text-xs text-amber-100/70 mt-3">
                      “Missing only” excludes every title whose known episode is currently present in Jellyfin, including a resolution-suffixed filename, plus titles with an existing planned output or partial. “Replacement copies” uses new collision-safe folders. “Deliberately replace” records your intent, but still rerips safely first and requires an exact second confirmation after verification. Nothing is deleted at this rip stage.
                    </div>
                    <button type="button" className="btn btn-secondary mt-3 mr-2 border-red-500/50 text-red-100" disabled={controlling} onClick={deleteStagedFilesAlreadyInJellyfin}>
                      Delete staged files already safely present in Jellyfin
                    </button>
                    <button type="button" className="btn btn-secondary mt-3 border-red-500/50 text-red-100" disabled={controlling} onClick={cancelRipPlanAndEject}>
                      Cancel this rip plan and eject disc
                    </button>
                  </div>
                )}
                {savedJob.state === 'authorized' && (
                  <div className="rounded-xl border border-blue-500/30 bg-blue-500/10 p-4">
                    <div className="font-semibold text-blue-100">Selection approved</div>
                    <div className="text-sm text-blue-100/70 mt-1">Add this disc to the rip queue. Other discs may be queued too.</div>
                    <button className="btn btn-primary mt-3" disabled={controlling} onClick={() => controlJob('start')}>Add disc to rip queue</button>
                  </div>
                )}
                {savedJob.state === 'queued' && (
                  <div id="rip-execution-confirmation" className="grid gap-3 scroll-mt-4">
                    <div className="font-semibold text-white">Ready to start MakeMKV</div>
                    <div className="rounded-lg border border-blue-500/25 bg-blue-500/10 p-3 text-sm text-blue-100">RipWeaver will use the MakeMKV executable saved in Settings and create a new private recovery-log folder automatically for this attempt.</div>
                    <label className="text-sm text-amber-200"><input type="checkbox" className="mr-2" checked={confirmPhysicalRip} onChange={(event) => setConfirmPhysicalRip(event.target.checked)} />I authorize MakeMKV to read this disc and rip exactly {preview.jobs.length} reviewed title(s). This is an authorization checkbox, not a partial-file setting.</label>
                    <label className="text-sm text-[var(--text-muted)]"><input type="checkbox" className="mr-2" checked={preserveFailedPartials} onChange={(event) => setPreserveFailedPartials(event.target.checked)} />Keep incomplete MKVs from earlier failed attempts for troubleshooting</label>
                    <div className="text-xs text-[var(--text-muted)]">
                      {failedCleanupPlan && failedCleanupPlan.file_count > 0
                        ? `Unchecked will remove ${failedCleanupPlan.file_count} exact incomplete MKV file(s) (${formatBytes(failedCleanupPlan.total_bytes)}) after a warning, then restart every selected title from the beginning.`
                        : 'No exact failed attempt was found. This will start every selected title from the beginning in clean isolated staging.'} Verified outputs are never removed here.
                    </div>
                    <button className="btn btn-primary" disabled={executingJobId === savedJob.job_id || !confirmPhysicalRip} onClick={() => controlJob('execute')}>
                      {executingJobId === savedJob.job_id
                        ? 'Starting this rip...'
                        : preserveFailedPartials
                        ? 'Start fresh rip and keep failed attempts'
                        : failedCleanupPlan && failedCleanupPlan.file_count > 0
                          ? 'Start ripping over the failed attempt'
                          : 'Start ripping this disc'}
                    </button>
                    <button type="button" className="btn btn-secondary" disabled={controlling} onClick={() => controlJob('return-to-review')}>Remove from rip queue and return to review</button>
                  </div>
                )}
                {['running', 'pause_requested'].includes(savedJob.state) && (
                  <div className="flex gap-3">
                    <button className="btn btn-secondary" disabled={controlling} onClick={() => controlJob('pause')}>Pause after active work</button>
                    <button className="btn btn-secondary" disabled={controlling} onClick={() => controlJob('stop')}>Stop queue</button>
                  </div>
                )}
              </div>
              <div className="mt-4 rounded-lg border border-[var(--border-color)] p-3 text-sm text-[var(--text-muted)]">
                Not ready? Leave this disc on hold and return later. To remove the physical disc, eject it manually after no read or rip is active.
              </div>
            </div>
          )}

          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            {preview.drives.map((drive) => (
              <div key={drive.disc_id} className="glass-panel rounded-xl p-5">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <div className="text-xs uppercase tracking-wider text-[var(--text-muted)]">{drive.disc_id}</div>
                    <div className="text-lg font-bold text-white mt-1">
                      {drive.strategy === 'single-open' ? 'Single-open MakeMKV' : 'Per-title fallback'}
                    </div>
                    <div className="text-sm text-[var(--text-muted)] mt-1">
                      {drive.selection_mode === 'reviewed-special-features'
                        ? 'Reviewed release catalogue matched — bonus titles and names attached'
                        : drive.selection_mode === 'automatic-bonus-fallback'
                        ? 'No episode cluster found — reviewing plausible bonus features'
                        : drive.selection_mode === 'bonus-features'
                          ? 'Bonus-feature selection'
                          : drive.selection_mode === 'mixed'
                            ? 'Main-title and bonus-feature selection'
                            : 'Episode selection'}
                    </div>
                    {drive.metadata_source && (
                      <div className={`mt-2 text-xs ${drive.metadata_status === 'matched' ? 'text-emerald-300' : 'text-amber-300'}`}>
                        {drive.metadata_status === 'matched'
                          ? `${drive.metadata_source === 'ripweaver-catalogue' ? 'RipWeaver Catalogue' : 'TheDiscDB'} matched ${drive.metadata_matched_title_count ?? 0} MakeMKV title${(drive.metadata_matched_title_count ?? 0) === 1 ? '' : 's'} by playlist`
                          : drive.metadata_status === 'support-required'
                            ? 'RipWeaver Catalogue automatic allowance is exhausted; manual continuation is available above'
                          : drive.metadata_status === 'not-found'
                            ? `${drive.metadata_source === 'ripweaver-catalogue' ? 'RipWeaver Catalogue' : 'TheDiscDB'} has no record for this disc yet`
                            : drive.metadata_status === 'title-mismatch'
                              ? `${drive.metadata_source === 'ripweaver-catalogue' ? 'RipWeaver Catalogue' : 'TheDiscDB'} found the disc, but its playlists did not safely match MakeMKV`
                              : `${drive.metadata_source === 'ripweaver-catalogue' ? 'RipWeaver Catalogue' : 'TheDiscDB'} lookup was unavailable; normal matching remains active`}
                      </div>
                    )}
                  </div>
                  <span className={`px-3 py-1 rounded-full text-xs font-bold ${drive.strategy === 'single-open'
                    ? 'bg-green-500/15 text-green-300'
                    : 'bg-blue-500/15 text-blue-300'
                  }`}>
                    {drive.title_count} titles
                  </span>
                </div>
                <div className="grid grid-cols-2 gap-3 mt-4 text-sm">
                  <div>
                    <div className="text-[var(--text-muted)]">Estimated size</div>
                    <div className="text-white font-semibold">{formatBytes(drive.estimated_bytes)}</div>
                  </div>
                  <div>
                    <div className="text-[var(--text-muted)]">MakeMKV minimum title length</div>
                    <div className="text-white font-semibold">
                      {drive.minimum_length_seconds === null ? 'Not applicable' : `${drive.minimum_length_seconds}s`}
                    </div>
                    {drive.selection_mode === 'reviewed-special-features' && drive.minimum_length_seconds !== null && (
                      <div className="text-xs text-[var(--text-muted)] mt-1">Titles shorter than this are excluded as menu/navigation material.</div>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>

          <div id="planned-title-list" className="glass-panel rounded-xl overflow-hidden scroll-mt-4">
            <div className="p-4 border-b border-[var(--border-color)] font-bold text-white">Planned titles</div>
            {selectingTitles && (
              <div className="p-4 border-b border-[var(--border-color)] bg-blue-500/10 space-y-3">
                <div className="text-sm text-blue-100">Check the exact titles you want to process. Selected: {selectedTitleIndexes.length} of {preview.jobs.length}.</div>
                <div className="flex flex-wrap gap-2">
                  <button type="button" className="btn btn-secondary" onClick={() => setSelectedTitleIndexes(preview.jobs.map((item) => item.title_index))}>Select all</button>
                  <button type="button" className="btn btn-secondary" onClick={() => setSelectedTitleIndexes([])}>Clear selection</button>
                  <button type="button" className="btn btn-primary" disabled={controlling || selectedTitleIndexes.length === 0} onClick={createSelectedTitleReview}>Create rip review for checked titles</button>
                  <button type="button" className="btn btn-secondary" disabled={controlling || selectedTitleIndexes.length === 0} onClick={restartExistingPipeline}>Restart matching for checked verified files</button>
                </div>
              </div>
            )}
            <div className="divide-y divide-[var(--border-color)]">
              {preview.jobs.map((job) => (
                <div key={job.job_id} className="p-4 grid grid-cols-1 lg:grid-cols-[8rem_1fr_8rem] gap-3 items-center">
                  <div className="font-mono text-sm text-white">
                    {selectingTitles && (
                      <input
                        type="checkbox"
                        className="mr-2"
                        checked={selectedTitleIndexes.includes(job.title_index)}
                        onChange={(event) => setSelectedTitleIndexes((current) => event.target.checked
                          ? [...current, job.title_index].sort((left, right) => left - right)
                          : current.filter((index) => index !== job.title_index))}
                      />
                    )}
                    Title {job.title_index}
                  </div>
                  <div className="min-w-0">
                    {currentTitleOutcome(job.title_index)?.display_name && (
                      <div className="mb-2 rounded-lg border border-blue-400/30 bg-blue-400/10 p-2 text-sm text-blue-100">
                        <div className="font-semibold">Current matched title: {currentTitleOutcome(job.title_index)?.display_name}</div>
                        <div className="text-xs mt-1">This is the newest durable identification result for this disc fingerprint and title index.</div>
                      </div>
                    )}
                    {job.prior_outcome_name && (
                      <div className="mb-2 rounded-lg border border-green-500/30 bg-green-500/10 p-2 text-sm text-green-100">
                        <div className="font-semibold">Previously matched as: {job.prior_outcome_name}</div>
                        <div className="text-xs mt-1">A prior verified run of this disc title produced this Jellyfin name.</div>
                        {job.prior_library_status === 'present' && <div className="text-xs mt-1 font-semibold">Present now in Jellyfin; held out of missing-only ripping.</div>}
                        {job.prior_library_status === 'missing' && <div className="text-xs mt-1 text-amber-200">The known episode is not currently present at this destination, so it remains eligible as missing.</div>}
                        {job.prior_library_status === 'unavailable' && <div className="text-xs mt-1 text-amber-200">The Jellyfin destination could not be checked safely; this title remains held for review.</div>}
                        {job.prior_library_relative && <div className="mt-2 break-all font-mono text-[11px] text-green-50">Historical Jellyfin path: {job.prior_library_relative}</div>}
                      </div>
                    )}
                    {job.display_name && <div className="text-sm font-semibold text-white">{job.display_name}</div>}
                    {job.extras_folder && <div className="text-xs text-indigo-200">Jellyfin extras category: {job.extras_folder}</div>}
                    {job.identification_status === 'evidence-required' && (
                      <div className="text-xs text-amber-300">Name remains held for fingerprint, image, OCR, or audio evidence after ripping.</div>
                    )}
                    {job.identification_status === 'disc-database-match' && (
                      <div className="text-xs text-emerald-300">Episode identified by exact TheDiscDB disc hash plus MakeMKV playlist and segment-map agreement.</div>
                    )}
                    <div className="text-sm text-[var(--text-muted)] truncate">
                      {job.final_destination || `Original collision-safe rip target: ${job.staging_destination}`}
                    </div>
                    <div className="text-xs text-[var(--text-muted)] mt-1">{formatBytes(job.estimated_bytes)}</div>
                    {job.prior_outcome_name && !pipelineQueue?.items.find((item) => item.media_id === job.job_id)?.display_name && (
                      <div className="mt-2 rounded-lg border border-blue-500/30 bg-blue-500/10 p-2 text-sm text-blue-100">
                        <div className="font-semibold">Previous result: {job.prior_outcome_name}</div>
                        {job.prior_episode_id && <div className="text-xs mt-1">Matched as {job.prior_episode_id}</div>}
                        {job.prior_library_relative && <div className="text-xs mt-1 break-all">Planned library name: {job.prior_library_relative}</div>}
                      </div>
                    )}
                  </div>
                  <div className={`text-xs font-bold uppercase ${job.collision_status === 'clear'
                    ? 'text-green-400'
                    : job.collision_status === 'not-checked'
                      ? 'text-slate-400'
                      : 'text-amber-400'
                  }`}>
                    {job.collision_status === 'library-exists'
                      ? 'already in Jellyfin — held'
                      : job.collision_status === 'library-check-unavailable'
                      ? 'Jellyfin check unavailable — held'
                      : existingRipsInPipeline && job.collision_status === 'final-exists'
                      ? 'existing MKV accepted for matching'
                      : job.collision_status}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {separatedHeldLibraryTitles.length > 0 && (
            <div className="rounded-xl border border-green-400/40 bg-green-500/10 p-4">
              <div className="font-bold text-green-100">Already in Jellyfin — held out of this rip</div>
              <div className="mt-1 text-sm text-green-100/75">
                RipWeaver found the same episode IDs in the expected Jellyfin season folders. Resolution-suffixed versions count as existing destinations and are not sent back to MakeMKV.
              </div>
              <div className="mt-3 grid gap-2 md:grid-cols-2">
                {separatedHeldLibraryTitles.map((held) => (
                  <div key={`${held.disc_fingerprint}-${held.title_index}`} className="rounded border border-green-300/25 bg-black/15 p-3 text-sm text-green-50">
                    <div className="font-semibold">Title {held.title_index}{held.episode_id ? ` · ${held.episode_id}` : ''}</div>
                    {held.outcome_name && <div className="mt-1 text-xs text-green-100/75">{held.outcome_name}</div>}
                  </div>
                ))}
              </div>
            </div>
          )}

          {(preview.skipped_titles ?? []).length > 0 && (
            <div className="rounded-xl border border-amber-400/40 bg-amber-500/10 p-4">
              <div className="font-bold text-amber-100">Titles remembered as skipped</div>
              <div className="mt-1 text-sm text-amber-100/75">
                These exact fingerprint/title combinations are excluded because of an explicit saved decision, such as removable content or a repeatedly unreadable title.
              </div>
              <div className="mt-3 space-y-2">
                {preview.skipped_titles.map((skipped) => (
                  <div key={`${skipped.disc_fingerprint}-${skipped.title_index}`} className="flex flex-wrap items-center justify-between gap-3 rounded border border-amber-300/25 bg-black/15 p-3">
                    <div>
                      <div className="font-semibold text-amber-50">Title {skipped.title_index} · {skipped.reason.replaceAll('_', ' ')}</div>
                      <div className="mt-1 text-xs text-amber-100/70">It will not be sent to MakeMKV while this saved decision remains active.</div>
                    </div>
                    <button type="button" className="btn btn-secondary text-xs" disabled={controlling} onClick={() => restoreSkippedDiscTitle(skipped)}>
                      Restore title to future rips
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {preview.skipped_discs.length > 0 && (
            <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
              <div className="font-bold text-amber-200">Excluded discs</div>
              {preview.skipped_discs.map((disc) => (
                <div key={disc.disc_id} className="text-sm text-amber-100/80 mt-2">
                  {disc.disc_id}: {disc.reasons.join(', ')}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default RipPipelineView;
