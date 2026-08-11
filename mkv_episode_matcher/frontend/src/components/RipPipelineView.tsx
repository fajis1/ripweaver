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
  staged_source_available: boolean;
  provisional_match: boolean;
  gemini_confidence: number | null;
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
  downstream_worker_limit: number;
  items: PipelineQueueItem[];
  title_dispositions?: Array<{
    disc_fingerprint: string;
    title_index: number;
    disposition: string;
    reason: string;
  }>;
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
  best_score: 'Best score',
  runner_up_score: 'Runner-up score',
  score: 'Score',
  margin: 'Decision margin',
  confidence: 'Provider confidence',
  candidate_episode_id: 'Candidate episode',
  candidate_scope: 'Candidate scope',
  candidate_count: 'Candidates compared',
  qualifying_window_count: 'Qualifying transcript windows',
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

const pipelineStages = ['rip', 'identify', 'transcode', 'organize'] as const;
const stageIcon: Record<string, string> = {
  rip: '💿', identify: '🔎', transcode: '🎞️', organize: '📁', complete: '✅',
};

const pipelineStatusLabel = (item: PipelineQueueItem) => {
  const action = {
    rip: { active: 'Ripping now', waiting: 'Waiting to rip' },
    identify: { active: 'Matching now', waiting: 'Waiting to match' },
    transcode: { active: 'Transcoding now', waiting: 'Waiting to transcode' },
    organize: { active: 'Transferring to Jellyfin now', waiting: 'Waiting to transfer to Jellyfin' },
  }[item.stage];
  if (item.state === 'running') return action?.active || `${item.stage} running`;
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
  const [existingRipsRestarted, setExistingRipsRestarted] = useState(false);
  const [selectedRecoveryCandidates, setSelectedRecoveryCandidates] = useState<Record<number, string>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [controlling, setControlling] = useState(false);
  const [geminiProgress, setGeminiProgress] = useState<Record<string, string>>({});
  const [silentVideoOcr, setSilentVideoOcr] = useState<Record<string, SilentVideoOcrResult>>({});
  const [silentVideoOcrErrors, setSilentVideoOcrErrors] = useState<Record<string, string>>({});
  const [silentVideoOcrRunningId, setSilentVideoOcrRunningId] = useState<string | null>(null);
  const [showLikelyRemovableOnly, setShowLikelyRemovableOnly] = useState(false);
  const [ejectingDrives, setEjectingDrives] = useState<number[]>([]);
  const [queuedEjectDrives, setQueuedEjectDrives] = useState<number[]>([]);
  const ejectQueueRef = useRef<Promise<void>>(Promise.resolve());
  const ejectDriveRef = useRef<(drive: DriveSlot, alreadyConfirmed?: boolean, quietFailure?: boolean) => Promise<boolean>>(
    () => Promise.resolve(false),
  );
  const [completeDiscAutoEjectDeadlines, setCompleteDiscAutoEjectDeadlines] = useState<Record<string, number>>({});
  const [completeDiscAutoEjectHolds, setCompleteDiscAutoEjectHolds] = useState<Record<string, 'kept' | 'failed'>>({});
  const [completeDiscAutoEjectClock, setCompleteDiscAutoEjectClock] = useState(() => Date.now());
  const completeDiscAutoEjectAttemptedRef = useRef<Set<string>>(new Set());
  const [renameDrafts, setRenameDrafts] = useState<Record<string, string>>({});
  const [bonusReviewModes, setBonusReviewModes] = useState<Record<string, boolean>>({});
  const [confirmPhysicalRip, setConfirmPhysicalRip] = useState(false);
  const [preserveFailedPartials, setPreserveFailedPartials] = useState(false);
  const [failedCleanupPlan, setFailedCleanupPlan] = useState<FailedRipCleanupPlan | null>(null);
  const [pipelineQueue, setPipelineQueue] = useState<PipelineQueue | null>(null);
  const [matchingPerformance, setMatchingPerformance] = useState<MatchingPerformanceRun[]>([]);
  const [learnedCoverage, setLearnedCoverage] = useState<LearnedSeriesCoverage | null>(null);
  const [transcodePlan, setTranscodePlan] = useState<TranscodeAuthorizationPlan | null>(null);
  const [organizationPlan, setOrganizationPlan] = useState<OrganizationAuthorizationPlan | null>(null);
  const [transcodeProfile, setTranscodeProfile] = useState('');
  const [jobDashboard, setJobDashboard] = useState<JobDashboard | null>(null);
  const [defaultProfile, setDefaultProfile] = useState('Default');
  const [rememberLastProfile, setRememberLastProfile] = useState(true);
  const [discSetups, setDiscSetups] = useState<Record<string, DiscSetup>>({});
  const [driveDashboard, setDriveDashboard] = useState<DriveDashboard | null>(null);
  const [refreshingDrives, setRefreshingDrives] = useState(false);
  const [showDriveMappingWizard, setShowDriveMappingWizard] = useState(false);
  const [mappingDraft, setMappingDraft] = useState<Record<string, 'trusted' | 'ignored'>>({});
  const [savingDriveMappings, setSavingDriveMappings] = useState(false);
  const [continueAfterMapping, setContinueAfterMapping] = useState(true);
  const reviewedMappingSnapshotRef = useRef<string | null>(null);
  const [preparingDrive, setPreparingDrive] = useState<number | null>(null);
  const [queuedPrepareDrives, setQueuedPrepareDrives] = useState<number[]>([]);
  const prepareQueue = useRef<Promise<void>>(Promise.resolve());
  const [handbrakeProfiles, setHandbrakeProfiles] = useState<StoredProfile[]>([]);
  const [automaticGeminiFallback, setAutomaticGeminiFallback] = useState(false);
  const [automaticEjectAfterCompletion, setAutomaticEjectAfterCompletion] = useState(false);
  const [discGeminiFallback, setDiscGeminiFallback] = useState(false);
  const [jellyfinRoots, setJellyfinRoots] = useState({ tv: '', movie: '' });
  const [unmatchedSeriesName, setUnmatchedSeriesName] = useState('');
  const [catalogueSupportStatus, setCatalogueSupportStatus] = useState<CatalogueSupportStatus | null>(null);
  const [supportAmountCents, setSupportAmountCents] = useState(1000);
  const [supportRateCents, setSupportRateCents] = useState(10);
  const [supportTermsAccepted, setSupportTermsAccepted] = useState(false);
  const [startingSupportCheckout, setStartingSupportCheckout] = useState(false);

  useEffect(() => {
    if (!savedJob || !['queued', 'running', 'pause_requested'].includes(savedJob.state)) return;
    const timer = window.setInterval(async () => {
      const response = await fetch(`/rip/jobs/${savedJob.job_id}`);
      if (response.ok) setSavedJob(await response.json());
    }, 2000);
    return () => window.clearInterval(timer);
  }, [savedJob]);

  useEffect(() => {
    const refresh = async () => {
      const [pipelineResponse, jobsResponse, performanceResponse] = await Promise.all([
        fetch('/rip/pipeline/items'),
        fetch('/rip/jobs'),
        fetch('/rip/pipeline/matching-performance'),
      ]);
      if (pipelineResponse.ok) setPipelineQueue(await pipelineResponse.json());
      if (jobsResponse.ok) setJobDashboard(await jobsResponse.json());
      if (performanceResponse.ok) {
        const payload = await performanceResponse.json() as { runs: MatchingPerformanceRun[] };
        setMatchingPerformance(payload.runs);
      }
      const drivesResponse = await fetch('/rip/drives');
      if (drivesResponse.ok) setDriveDashboard(await drivesResponse.json());
    };
    void refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => window.clearInterval(timer);
  }, []);

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
          ? `Queue resumed. ${runnable.length} queued item(s) are available to an authorized stage worker.`
          : held.length > 0
            ? `Queue is active, but ${held.length} item(s) require review: ${holdCodes.join(', ')}. Use “Show required choices” below; resume cannot bypass these holds.`
            : 'Queue is active. No unfinished authorized work is waiting.');
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

  const restoreSkippedDiscTitle = async (skipped: RipPreview['skipped_titles'][number]) => {
    const drive = selectedDriveSlot;
    if (!drive || !savedJob) {
      setError('The loaded disc review is no longer available. Refresh the drives and try again.');
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
      if (savedJob.state === 'awaiting_review') {
        const cancelResponse = await fetch(`/rip/jobs/${savedJob.job_id}/cancel`, {
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
    setControlling(true);
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
      setControlling(false);
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

  const playReview = async (mediaId: string) => {
    if (!window.confirm('Open this exact recorded MKV in the Windows default media player for review? No file will be changed.')) return;
    const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(mediaId)}/play-review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm_play: true }) });
    const payload = await response.json(); if (!response.ok) setError(payload.detail || 'Review playback could not start.');
  };

  const analyzeSilentVideo = async (item: PipelineQueueItem) => {
    if (!window.confirm(`Read six bounded frames from "${item.display_name || item.media_id}" and run local OCR? The MKV will not be changed or deleted.`)) return;
    setControlling(true);
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
      setControlling(false);
    }
  };

  const saveManualEpisodeIdentification = async (item: PipelineQueueItem) => {
    const newName = (renameDrafts[item.media_id] || '').trim();
    const isBonus = Boolean(bonusReviewModes[item.media_id]);
    if (!newName) return;
    const destinationSummary = isBonus
      ? `the canonical series Extras folder as "${newName}.mkv"`
      : `the reviewed episode identity "${newName}.mkv"`;
    if (!window.confirm(`Use ${destinationSummary}? RipWeaver will preserve the original staged file, remember this title for the disc fingerprint, and continue the pipeline.`)) return;
    setControlling(true);
    setError('');
    try {
      const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(item.media_id)}/manual-episode-identification`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_name: newName, content_type: isBonus ? 'bonus' : 'episode', confirm_identification: true }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Manual episode identification could not be saved.');
      setRenameDrafts((current) => ({ ...current, [item.media_id]: '' }));
      setBonusReviewModes((current) => ({ ...current, [item.media_id]: false }));
      const refreshed = await fetch('/rip/pipeline/items');
      if (refreshed.ok) setPipelineQueue(await refreshed.json());
      setReviewNotice(isBonus
        ? 'Saved the reviewed TV bonus identity. It will use the canonical series Extras folder in Jellyfin.'
        : 'Saved the reviewed episode identity and returned the staged rip to the automatic pipeline. The .mkv extension is preserved.');
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'Manual episode identification could not be saved.';
      setError(message);
      window.alert(message);
    } finally {
      setControlling(false);
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
    setControlling(true);
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
      setControlling(false);
    }
  };

  const runPreparedDrive = async (
    drive: DriveSlot,
    setup: DiscSetup,
    catalogueLookupMode: 'automatic' | 'manual' = 'automatic',
  ) => {
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
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Disc pipeline could not be prepared safely.');
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
      .then(() => runPreparedDrive(drive, setup));
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
      .then(() => runPreparedDrive(
        selectedDriveSlot,
        getDiscSetup(driveSetupKey(selectedDriveSlot)),
        'manual',
      ));
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

  const selectDriveJob = (job: OrchestrationJob) => {
    if (!job.preview) return;
    setSavedJob(job);
    setPreview(job.preview);
    setConfirmPhysicalRip(false);
    setError('');
    setReviewNotice('');
    setSelectingTitles(false);
    setSelectedTitleIndexes(job.preview.jobs.map((item) => item.title_index));
    setExistingRecoveryPlan(null);
    setExistingRipsRestarted(false);
    window.requestAnimationFrame(() => {
      document.getElementById('selected-disc-review')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  };

  const controlJob = async (action: 'authorize' | 'start' | 'execute' | 'pause' | 'stop' | 'return-to-review') => {
    if (!savedJob) return;
    setControlling(true);
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
      setControlling(false);
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
      && visiblePipelineItems.some((item) => item.stage === 'identify' && item.state === 'review_required' && [
        'missing_season_context',
        'unmatched_disc_analysis_required',
        'all_season_analysis_failed',
        'all_season_series_not_found',
        'all_season_evidence_failed',
        'all_season_catalog_unavailable',
        'all_season_sequence_review_required',
      ].includes(item.review_code || '')),
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
    const currentFingerprint = drive?.current_disc_fingerprint;
    if (currentFingerprint) {
      const continuation = (jobDashboard?.jobs ?? []).find((candidate) => (
        ['awaiting_review', 'authorized', 'queued', 'running', 'pause_requested', 'paused'].includes(candidate.state)
        && candidate.preview?.jobs.some((job) => job.staging_destination.includes(`/${currentFingerprint}/`))
      ));
      if (continuation) return continuation;
    }
    const currentJobId = drive?.current_job_id;
    if (!currentJobId) return undefined;
    return (jobDashboard?.jobs ?? []).find((candidate) => candidate.job_id === currentJobId);
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
      if (
        !job
        || job.state !== 'awaiting_review'
        || !allKnownTitlesAlreadyInLibrary(job.preview)
      ) return [];
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
  }, [automaticEjectAfterCompletion, completeDiscAutoEjectHolds, driveDashboard?.drives, jobDashboard?.jobs, latestJobForDrive]);

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
    const titleMatch = item.media_id.match(/-title-(\d{3})(?:-|$)/);
    const key = item.disc_fingerprint && titleMatch
      ? `${item.disc_fingerprint}:${titleMatch[1]}`
      : item.media_id;
    const previous = items.get(key);
    const discardedWouldHideActive = item.state === 'discarded' && previous && previous.state !== 'discarded';
    const activeReplacesDiscarded = previous?.state === 'discarded' && item.state !== 'discarded';
    if (!discardedWouldHideActive && (!previous || activeReplacesDiscarded || Date.parse(item.updated_at) >= Date.parse(previous.updated_at))) items.set(key, item);
    return items;
  }, new Map<string, PipelineQueueItem>()).values());
  const activePipelineItems = latestPipelineItems.filter((item) => !['completed', 'discarded'].includes(item.state));
  const likelyRemovableCount = activePipelineItems.filter((item) => item.likely_removable).length;
  const visiblePipelineItems = activePipelineItems
    .filter((item) => !showLikelyRemovableOnly || item.likely_removable)
    .sort((left, right) => Number(right.likely_removable) - Number(left.likely_removable));
  const visibleRipJobs = (jobDashboard?.jobs ?? []).filter((job) => {
    if (!['queued', 'running', 'pause_requested'].includes(job.state)) return false;
    if (queueOnly) return true;
    return Boolean(savedJob?.job_id && job.job_id === savedJob.job_id);
  });
  const existingRipsNeedAnalysis = visiblePipelineItems.some((item) =>
    item.stage === 'identify'
    && item.state === 'review_required'
    && ['missing_season_context', 'unmatched_disc_analysis_required', 'all_season_analysis_failed', 'all_season_series_not_found', 'all_season_evidence_failed', 'all_season_catalog_unavailable', 'all_season_sequence_review_required'].includes(item.review_code || '')
  );
  const existingRipsInPipeline = existingRipsRestarted || visiblePipelineItems.some(
    (item) => item.disc_fingerprint === selectedDiscFingerprint && item.media_id.includes('-recovery-'),
  );
  const currentTitleOutcome = (titleIndex: number) => latestPipelineItems.find((item) => {
    const match = item.media_id.match(/-title-(\d{3})(?:-|$)/);
    return match !== null && Number(match[1]) === titleIndex;
  });
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
  const suggestedUnmatchedSeries = selectedDriveLabel
    .replace(/[_-]+/g, ' ')
    .replace(/\b(?:DVD|DISC|DISK|VOLUME|VOL)\s*\d+\b.*$/i, '')
    .replace(/\bCSR\s+DIM\s*\d+\b.*$/i, '')
    .replace(/\bSEASON\s*\d+\b.*$/i, '')
    .replace(/\s+\d+\s*$/i, '')
    .replace(/\s+/g, ' ')
    .trim();
  const explicitSeasonMatch = selectedDriveLabel.match(/\bSEASON\s*(\d{1,2})\b/i);
  const suggestedSeason = explicitSeasonMatch ? Number(explicitSeasonMatch[1]) : null;
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
    const scopeLabel = suggestedSeason === null ? 'across every aired season' : `against Season ${suggestedSeason}`;
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
          confirm_media_read: true,
          confirm_provider_lookup: true,
          confirm_external_fallback: discGeminiFallback,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'All-season analysis could not be started.');
      setReviewNotice(`Started ${suggestedSeason === null ? 'all-season' : `Season ${suggestedSeason}`} evidence and sequence analysis for ${payload.item_count} title(s).`);
      const queueResponse = await fetch('/rip/pipeline/items');
      if (queueResponse.ok) setPipelineQueue(await queueResponse.json());
    } catch (requestError) {
      setReviewNotice(requestError instanceof Error ? requestError.message : 'All-season analysis could not be started.');
    } finally {
      setControlling(false);
    }
  };
  const analyzeHeldItemAsTv = async (item: PipelineQueueItem) => {
    const fingerprint = item.disc_fingerprint;
    const recoveredSeries = item.media_id
      .split('--disc-', 1)[0]
      .replace(/[_-]+/g, ' ')
      .replace(/\s+\d+\s*$/i, '')
      .replace(/\s+/g, ' ')
      .trim();
    const reviewedSeries = suggestedUnmatchedSeries || recoveredSeries;
    if (!fingerprint || !reviewedSeries) {
      setReviewNotice('Open this disc on the Disc Dashboard and enter its canonical TV series name.');
      return;
    }
    if (!window.confirm(`Retry the held titles from this disc as TV episodes of “${reviewedSeries}”? RipWeaver will compare bounded local evidence against the aired episode catalogue across all seasons. It will not rerip, rename, move, delete, or transcode media.`)) return;
    setControlling(true);
    setError('');
    try {
      const response = await fetch('/rip/pipeline/analyze-unmatched-disc', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          disc_fingerprint: fingerprint,
          series_name: reviewedSeries,
          season: suggestedSeason,
          confirm_media_read: true,
          confirm_provider_lookup: true,
          confirm_external_fallback: automaticGeminiFallback,
        }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'TV episode analysis could not be started.');
      setReviewNotice(`Started TV episode analysis for ${payload.item_count} held title(s) as “${reviewedSeries}”.`);
      const queueResponse = await fetch('/rip/pipeline/items');
      if (queueResponse.ok) setPipelineQueue(await queueResponse.json());
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'TV episode analysis could not be started.';
      setError(message);
      setReviewNotice(message);
    } finally {
      setControlling(false);
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
      <div>
        <h2 className="text-3xl font-bold heading-gradient mb-1">Disc Dashboard</h2>
        <p className="text-sm text-[var(--text-muted)]">
          See every detected optical drive, configure each inserted disc, and follow it through the pipeline.
        </p>
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
                {jobDashboard.automatic_processing_enabled ? 'Automatic processing requested' : 'Automatic processing disabled'} · {driveDashboard?.status === 'ready' ? 'drive status refreshed' : 'waiting for read-only refresh'}
              </div>
            </div>
            <button type="button" className="btn btn-secondary" onClick={() => void refreshDrives()} disabled={refreshingDrives || driveDashboard?.refresh_in_progress}>
              {refreshingDrives || driveDashboard?.refresh_in_progress ? 'Reading drive slots (up to 2 minutes)…' : 'Refresh drives (read-only)'}
            </button>
          </div>
          {driveDashboard?.status !== 'ready' && (
            <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-100">
              <div>
                {driveDashboard?.refresh_in_progress
                  ? driveDashboard.error_code === 'timeout'
                    ? 'A new read-only MakeMKV discovery is running now. The previous attempt timed out; its recovery message will be replaced when this retry finishes.'
                    : 'Read-only MakeMKV drive discovery is running now. Keep the optical drives connected while RipWeaver waits for their slot information.'
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
                  <button type="button" className="btn btn-secondary text-xs" onClick={() => void refreshDrives(120)} disabled={refreshingDrives || driveDashboard?.refresh_in_progress}>
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
            {!driveDashboard || driveDashboard.drives.filter((drive) => !drive.mapping_status || drive.mapping_status === 'trusted').length === 0 ? (
              <div className="rounded-xl border border-dashed border-[var(--border-color)] p-8 text-center">
                <div className="text-5xl opacity-40 mb-3">▱</div>
                <div className="font-semibold text-white">No trusted optical drives</div>
                <div className="text-sm text-[var(--text-muted)] mt-1">Refresh, then open the drive setup wizard to approve the optical devices you intend to use.</div>
              </div>
            ) : driveDashboard.drives.filter((drive) => !drive.mapping_status || drive.mapping_status === 'trusted').map((drive) => {
              const driveKey = driveSetupKey(drive);
              const setup = getDiscSetup(driveKey);
              const job = latestJobForDrive(drive.drive_index);
              const driveJobs = (jobDashboard?.jobs ?? []).filter((candidate) => candidate.preview?.drives.some((item) => item.drive_index === drive.drive_index));
              const currentDiscJobs = drive.current_disc_fingerprint
                ? driveJobs.filter((candidate) => previewHasDiscFingerprint(candidate.preview, drive.current_disc_fingerprint))
                : [];
              const failedRipJob = currentDiscJobs.find((candidate) => (
                candidate.state === 'failed'
                && (candidate.rip_title_summary?.unfinished_titles ?? []).some((title) => title.drive_index === drive.drive_index)
              ));
              const earlierActiveJob = driveJobs.find((candidate) => candidate.job_id !== job?.job_id && ['authorized', 'queued', 'running', 'pause_requested'].includes(candidate.state));
              const driveFingerprint = drive.current_disc_fingerprint ?? job?.preview?.jobs.map((item) => item.staging_destination.match(/(?:^|\/)([0-9a-f]{16})(?:\/|$)/)?.[1]).find((value): value is string => Boolean(value));
              const drivePipelineItems = (pipelineQueue?.items ?? []).filter((item) => driveFingerprint && item.disc_fingerprint === driveFingerprint);
              const futureSkipApiReady = Array.isArray(pipelineQueue?.title_dispositions);
              const skippedTitleIndexes = new Set((pipelineQueue?.title_dispositions ?? [])
                .filter((item) => driveFingerprint && item.disc_fingerprint === driveFingerprint && item.disposition === 'skip')
                .map((item) => item.title_index));
              const safelyPresentTitleIndexes = new Set([
                ...drivePipelineItems
                  .filter((item) => item.staged_source_available && typeof item.title_index === 'number')
                  .map((item) => item.title_index as number),
                ...currentDiscJobs.flatMap((candidate) => (
                  candidate.preview?.jobs
                    .filter((item) => item.prior_library_status === 'present')
                    .map((item) => item.title_index) ?? []
                )),
              ]);
              const currentReviewRecoveryJob = (job?.state === 'awaiting_review' || (job?.state === 'queued' && job.executor_attached === false))
                && safelyPresentTitleIndexes.size > 0
                && currentDiscJobs.some((candidate) => candidate.job_id === job.job_id)
                ? job
                : undefined;
              const reripPlanJob = failedRipJob ?? currentReviewRecoveryJob;
              const missingRipTitleIndexes = reripPlanJob?.preview?.jobs
                .filter((item) => item.drive_index === drive.drive_index && !safelyPresentTitleIndexes.has(item.title_index) && !skippedTitleIndexes.has(item.title_index))
                .map((item) => item.title_index) ?? [];
              const reripJob = missingRipTitleIndexes.length > 0 ? reripPlanJob : undefined;
              const missingRipTitleCount = missingRipTitleIndexes.length;
              const recoveryKnownTitleIndexes = new Set([
                ...(reripPlanJob?.preview?.jobs
                  .filter((item) => item.drive_index === drive.drive_index)
                  .map((item) => item.title_index) ?? []),
                ...safelyPresentTitleIndexes,
              ]);
              const recoveryKnownTitleCount = recoveryKnownTitleIndexes.size;
              const recoveryPresentTitleCount = [...recoveryKnownTitleIndexes]
                .filter((titleIndex) => safelyPresentTitleIndexes.has(titleIndex)).length;
              const recoverySkippedTitleCount = [...recoveryKnownTitleIndexes]
                .filter((titleIndex) => skippedTitleIndexes.has(titleIndex)).length;
              const activeRipTitleMatch = job?.state === 'running' ? job.rip_progress_scope?.match(/title-(\d+)$/) : null;
              const activeRipTitleIndex = activeRipTitleMatch ? Number.parseInt(activeRipTitleMatch[1], 10) : null;
              const activeRipTitleIsSkipped = activeRipTitleIndex !== null && skippedTitleIndexes.has(activeRipTitleIndex);
              const failedRipTitleMatch = reripJob?.state === 'failed' ? reripJob.rip_progress_scope?.match(/title-(\d+)$/) : null;
              const failedRipTitleIndex = failedRipTitleMatch ? Number.parseInt(failedRipTitleMatch[1], 10) : null;
              const failedRipTitleIsSkipped = failedRipTitleIndex !== null && skippedTitleIndexes.has(failedRipTitleIndex);
              const identificationNeedsAttention = drivePipelineItems.some((item) => item.stage === 'identify' && ['failed', 'review_required'].includes(item.state));
              const discardedIdentificationRemains = drivePipelineItems.some((item) => item.stage === 'identify' && item.state === 'discarded' && item.staged_source_available);
              const driveFailed = Boolean(reripJob) || (job?.state === 'failed' && (job.failed_drive_indexes?.length === 0 || job.failed_drive_indexes?.includes(drive.drive_index)));
              const driveAlreadyComplete = job?.state === 'awaiting_review' && allKnownTitlesAlreadyInLibrary(job.preview);
              const driveNeedsReview = job?.state === 'awaiting_review' && !driveAlreadyComplete;
              const drivePaused = job?.state === 'paused';
              const interruptedQueued = job?.state === 'queued' && job.executor_attached === false && job.rip_progress_percent !== null && job.rip_progress_percent !== undefined;
              const driveNeedsAction = Boolean(reripJob) || driveNeedsReview || drivePaused || job?.state === 'queued' || identificationNeedsAttention || discardedIdentificationRemains || Boolean(earlierActiveJob);
              const driveStatus = reripJob
                ? `rerip ${missingRipTitleCount} missing ${missingRipTitleCount === 1 ? 'title' : 'titles'}`
                : identificationNeedsAttention
                  ? 'identification needs attention'
                : discardedIdentificationRemains
                  ? 'unidentified rip preserved'
                  : earlierActiveJob
                    ? 'eject held by earlier rip job'
                    : driveAlreadyComplete
                      ? 'already complete in Jellyfin'
                    : job?.state === 'completed'
                      ? 'rip completed'
                      : job?.state.replaceAll('_', ' ') ?? 'disc inserted';
              const selectedDriveJob = savedJob?.job_id === job?.job_id;
              const completeAutoEjectKey = job && driveAlreadyComplete ? completeDiscAutoEjectKey(drive, job) : null;
              const completeAutoEjectDeadline = completeAutoEjectKey ? completeDiscAutoEjectDeadlines[completeAutoEjectKey] : undefined;
              const completeAutoEjectSeconds = completeAutoEjectDeadline === undefined
                ? null
                : Math.max(0, Math.ceil((completeAutoEjectDeadline - completeDiscAutoEjectClock) / 1000));
              const completeAutoEjectHold = completeAutoEjectKey ? completeDiscAutoEjectHolds[completeAutoEjectKey] : undefined;
              const stagingAttemptCollision = job?.state === 'awaiting_review'
                && (job.preview?.collision_count ?? 0) > 0
                && job.preview?.jobs.every((item) => ['clear', 'staging-exists'].includes(item.collision_status));
              return (
                <div key={driveKey} className={`rounded-xl border p-4 space-y-4 ${driveNeedsAction || driveFailed
                  ? 'border-red-500/60 bg-red-500/10'
                  : selectedDriveJob
                    ? 'border-blue-400/60 bg-blue-500/10'
                    : 'border-[var(--border-color)] bg-[var(--bg-primary)]/40'
                }`}>
                  <div className="flex items-center justify-between gap-3">
                    <span className={`text-4xl ${drive.has_disc ? 'text-blue-300' : 'text-slate-600'}`} aria-label={drive.has_disc ? 'Disc inserted' : 'Empty tray'}>{drive.has_disc ? '●' : '▱'}</span>
                    <div className="min-w-0 flex-1">
                      <div className="font-semibold text-white">Optical drive {drive.drive_index + 1}{drive.display_name ? ` · ${drive.display_name}` : ''}{drive.disc_label ? ` — ${drive.disc_label}` : ''}</div>
                      <div className={`text-xs font-bold uppercase ${driveNeedsAction || driveFailed ? 'text-red-300' : drive.has_disc ? 'text-blue-300' : 'text-slate-400'}`}>{drive.has_disc ? driveStatus : 'empty tray'}</div>
                    </div>
                  </div>
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
                  {drive.has_disc && (Boolean(reripJob) || identificationNeedsAttention) && (
                    <div className={`rounded-lg border p-3 text-sm space-y-2 ${reripJob?.state === 'failed' ? 'border-red-500/40 bg-red-500/15 text-red-100' : 'border-amber-400/40 bg-amber-500/15 text-amber-100'}`}>
                      {reripJob ? (
                        <>
                          <div className="font-semibold">
                            {reripJob.state === 'failed'
                              ? `Rip stopped safely${reripJob.error_category ? ` · ${reripJob.error_category.replaceAll('_', ' ')}` : ''}`
                              : reripJob.state === 'queued'
                                ? 'Missing-title rerip is queued but has not started'
                                : 'Missing-title rerip ready'}
                          </div>
                          <div className="rounded border border-amber-300/30 bg-black/10 p-2">
                            <div className="font-semibold">Current recovery status: {recoveryPresentTitleCount} of {recoveryKnownTitleCount} titles are already safely present in staging or Jellyfin.</div>
                            {recoverySkippedTitleCount > 0 && <div className="mt-1">{recoverySkippedTitleCount} {recoverySkippedTitleCount === 1 ? 'title is' : 'titles are'} intentionally skipped for this exact disc.</div>}
                            <div className="mt-1">Only {missingRipTitleCount} missing {missingRipTitleCount === 1 ? 'title needs' : 'titles need'} to be reripped.</div>
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
                        <div>Ripping finished, but one or more verified titles still need identification review. Reripping those files would not improve their identification evidence.</div>
                      )}
                      <button
                        type="button"
                        className={reripJob ? 'btn btn-primary text-xs' : 'btn btn-secondary text-xs'}
                        disabled={controlling || Boolean(earlierActiveJob)}
                        onClick={() => reripJob ? void reripMissingItems(reripJob, missingRipTitleIndexes) : job && selectDriveJob(job)}
                      >
                        {reripJob
                          ? reripJob.state === 'queued'
                            ? `Start queued ${missingRipTitleCount}-title rerip`
                            : `Rerip ${missingRipTitleCount} missing ${missingRipTitleCount === 1 ? 'title' : 'titles'}`
                          : 'Review identification results'}
                      </button>
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
                  {drive.has_disc && driveAlreadyComplete && job?.preview && (
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
                      {completeAutoEjectHold === 'failed' && <div className="text-xs text-amber-100">Automatic eject was refused safely. Use Eject disc after checking that no drive work is active.</div>}
                    </div>
                  )}
                  {drive.available && (
                    <button type="button" className="btn btn-secondary" disabled={ejectingDrives.includes(drive.drive_index) || queuedEjectDrives.includes(drive.drive_index) || ['authorized', 'queued', 'running', 'pause_requested'].includes(job?.state || '')} onClick={() => { if (completeAutoEjectKey) keepCompletedDiscInserted(completeAutoEjectKey); void ejectDrive(drive); }}>
                      {ejectingDrives.includes(drive.drive_index) ? (drive.has_disc ? 'Ejecting…' : 'Opening tray…') : queuedEjectDrives.includes(drive.drive_index) ? (drive.has_disc ? 'Queued to eject' : 'Queued to open') : drive.has_disc ? 'Eject disc' : 'Open tray'}
                    </button>
                  )}
                  {drive.has_disc && (
                    <label className="block rounded-lg border border-blue-500/25 bg-blue-500/10 p-3 text-sm text-blue-100">
                      <input type="checkbox" className="mr-2" checked={setup.addMissingOnly} onChange={(event) => updateDiscSetup(driveKey, { addMissingOnly: event.target.checked })} />
                      Add missing items from this disc
                      <span className="mt-1 block text-xs text-blue-100/70">Check known results during preparation and check Jellyfin again immediately after every match. Existing destinations are held individually for review while missing episodes, extras, editions, commentary variants, and unrelated titles continue.</span>
                    </label>
                  )}
                  {drive.has_disc && !reripJob && !identificationNeedsAttention && !discardedIdentificationRemains && (!job || ['completed', 'failed'].includes(job.state) || stagingAttemptCollision) && (
                    <button
                      type="button"
                      className="btn btn-primary w-full"
                      disabled={preparingDrive === drive.drive_index || queuedPrepareDrives.includes(drive.drive_index)}
                      onClick={() => queueDrivePipeline(drive, setup)}
                    >
                      {preparingDrive === drive.drive_index
                        ? 'Reading disc and preparing plan…'
                        : queuedPrepareDrives.includes(drive.drive_index)
                          ? 'Queued for preparation'
                          : stagingAttemptCollision
                            ? 'Prepare fresh isolated attempt'
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
                  {drive.has_disc && !reripJob && job?.preview && (['awaiting_review', 'authorized', 'queued', 'failed'].includes(job.state) || identificationNeedsAttention || discardedIdentificationRemains) && (
                    <button type="button" disabled={controlling} className={driveNeedsReview || driveFailed ? 'btn btn-primary w-full' : 'btn btn-secondary w-full'} onClick={() => selectDriveJob(job)}>
                      {selectedDriveJob
                        ? 'This disc is shown below'
                        : driveAlreadyComplete
                          ? 'View completed-disc options'
                          : driveNeedsReview
                            ? 'Review this disc'
                            : driveFailed
                              ? 'Review error and retry options'
                              : 'Open this disc’s controls'}
                    </button>
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
              <div className="font-bold text-white">{attentionOnly ? 'Pipeline errors and review choices' : queueOnly ? 'Global downstream queue' : 'Selected disc queue'}</div>
              <div className="text-sm text-[var(--text-muted)]">
                {attentionOnly ? 'Items that need a decision are collected here without occupying an optical drive or blocking unrelated work.' : 'One global worker serializes authorized stages. Identification runs automatically; transcoding and organization use validated automatic or reviewed authorization.'}
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
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
              {!queueOnly && !attentionOnly && visiblePipelineItems.length === 0 && savedJob?.state === 'queued' ? (
                <button type="button" className="btn btn-primary" onClick={() => document.getElementById('rip-execution-confirmation')?.scrollIntoView({ behavior: 'smooth', block: 'center' })}>
                  Continue to rip confirmation
                </button>
              ) : (
                <>
                  {!attentionOnly && <button type="button" className="btn btn-primary" disabled={controlling} onClick={() => controlPipeline('resume')}>
                    {queueOnly ? 'Start / resume authorized work' : 'Start / resume downstream work'}
                  </button>}
                  {!attentionOnly && <button type="button" className="btn btn-secondary" disabled={pipelineQueue.paused} onClick={() => controlPipeline('pause')}>
                    {pipelineQueue.paused ? 'Downstream queue paused' : 'Pause downstream queue'}
                  </button>}
                </>
              )}
            </div>
          </div>
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
          {!queueOnly && !attentionOnly && visiblePipelineItems.some((item) => ['missing_season_context', 'unmatched_disc_analysis_required', 'all_season_analysis_failed', 'all_season_series_not_found', 'all_season_evidence_failed', 'all_season_catalog_unavailable', 'all_season_sequence_review_required'].includes(item.review_code || '')) && (
            <div className="rounded-lg border border-indigo-400/30 bg-indigo-400/10 p-3 text-sm text-indigo-100 space-y-2">
              <div className="font-semibold">{suggestedSeason === null ? 'Run general all-season episode matching' : `Run Season ${suggestedSeason} episode matching`}</div>
              <p>{suggestedSeason === null ? 'The disc has no reliable season context. The matcher will compare the complete ordered disc sequence against every aired episode for the reviewed series.' : `The disc label explicitly identifies Season ${suggestedSeason}. The matcher will restrict candidates to that season.`} It transcribes each title once, queues confident results, and does not use a saved disc layout.</p>
              <label className="block text-xs text-indigo-100">Canonical TV series name
                <input
                  className="mt-1 w-full rounded-lg bg-[var(--bg-primary)] border border-indigo-400/30 p-2 text-white"
                  value={unmatchedSeriesName}
                  onChange={(event) => setUnmatchedSeriesName(event.target.value)}
                  placeholder={suggestedUnmatchedSeries || 'Enter the TV series name'}
                />
              </label>
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
          {visiblePipelineItems.some((item) => item.stage === 'transcode' && item.state === 'queued') && (
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
          {transcodePlan && (
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
          {visiblePipelineItems.some((item) => item.stage === 'organize' && item.state === 'queued') && !organizationPlan && (
            <div className="rounded-lg border border-blue-400/30 bg-blue-400/10 p-3 text-sm text-blue-100 space-y-3">
              <p>Verified encodes are ready in staging. Review their exact Jellyfin destinations and confirm a collision-refusing move.</p>
              <button type="button" className="btn btn-primary" disabled={controlling} onClick={reviewOrganizationAuthorization}>
                Review placement into Jellyfin
              </button>
            </div>
          )}
          {organizationPlan && (
            <div className="rounded-lg border border-indigo-400/30 bg-indigo-400/10 p-4 space-y-3 text-sm">
              <div className="font-semibold text-white">Reviewed Jellyfin placement</div>
              <div>{organizationPlan.item_count} exact verified encode(s): {organizationPlan.tv_count} TV, {organizationPlan.movie_count} movie/bonus feature.</div>
              <div>{organizationPlan.collision_count === 0 ? 'No destination collisions detected.' : `${organizationPlan.collision_count} destination collision(s) require separate review.`}</div>
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
              {visiblePipelineItems.some((item) => ['missing_season_context', 'unmatched_disc_analysis_required', 'all_season_analysis_failed', 'all_season_series_not_found', 'all_season_evidence_failed', 'all_season_catalog_unavailable', 'all_season_sequence_review_required'].includes(item.review_code || '')) && (
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
                <div key={item.media_id} className="py-3 flex flex-wrap items-center justify-between gap-3">
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
                      {pipelineStatusLabel(item)}
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
                      Transcoding and verification finished. Waiting for the global worker to transfer this file to Jellyfin. No destination collision has been detected.
                    </div>
                  )}
                  {item.stage === 'organize' && item.state === 'completed' && (
                    <div className="max-w-md rounded-lg border border-green-400/30 bg-green-400/10 p-3 text-xs text-green-100">
                      <div>Moved into Jellyfin on {new Date(item.updated_at).toLocaleString()}.</div>
                      {formatBytes(item.output_size_bytes) && <div className="mt-1">Finished file size: {formatBytes(item.output_size_bytes)}</div>}
                      {item.retained_source_available && <div className="mt-1">Original retained in staging for deletion/reprocessing.</div>}
                      {fullLibraryPath(item) && <div className="mt-2 break-all font-mono text-[11px] text-green-50">{fullLibraryPath(item)}</div>}
                      {item.provisional_match && <div className="mt-3 rounded border border-amber-400/30 bg-amber-400/10 p-3 text-amber-100"><div>Gemini provisional match{item.gemini_confidence !== null ? ` · ${Math.round(item.gemini_confidence * 100)}% confidence` : ''}. Review and rename it if needed.</div><div className="mt-2 flex flex-wrap gap-2"><button type="button" className="btn btn-secondary text-xs" onClick={() => playReview(item.media_id)}>Play for review</button><input className="input-field min-w-64 text-xs" value={renameDrafts[item.media_id] || ''} onChange={(event) => setRenameDrafts((current) => ({ ...current, [item.media_id]: event.target.value }))} placeholder="New filename (without .mkv)" /><button type="button" className="btn btn-primary text-xs" disabled={!renameDrafts[item.media_id]?.trim()} onClick={() => renameProvisional(item.media_id)}>Rename reviewed file</button></div></div>}
                    </div>
                  )}
                  {['failed', 'review_required'].includes(item.state) && (
                    item.review_code === 'library_collision' ? (
                      <div className="max-w-md rounded-lg border border-red-400/30 bg-red-400/10 p-3 text-xs text-red-100">
                        <div>An existing Jellyfin episode conflicts with this matched title. Choose exactly what happens to the new pipeline media.</div>
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
                    ) : ['missing_season_context', 'unmatched_disc_analysis_required', 'all_season_analysis_failed', 'all_season_series_not_found', 'all_season_evidence_failed', 'all_season_catalog_unavailable', 'all_season_sequence_review_required'].includes(item.review_code || '') ? (
                      <div className="max-w-md rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-100">
                        <div>This title needs episode-sequence analysis before it can continue.</div>
                        {item.review_code === 'all_season_catalog_unavailable' && <div className="mt-2">TMDb did not return an aired episode catalogue for the reviewed series name. Open the disc dashboard, confirm the canonical series name, and retry.</div>}
                        {item.review_code === 'all_season_series_not_found' && <div className="mt-2">No TMDb television series matched the canonical series name. Correct the series name on the Disc Dashboard and retry.</div>}
                        {item.review_code === 'all_season_evidence_failed' && <div className="mt-2">Audio evidence collection failed before episode matching began. Review the server diagnostic for the safe failure category, then retry.</div>}
                        <div className="mt-3 flex flex-wrap gap-2">
                          {item.staged_source_available && (
                            <>
                              <button type="button" className="btn btn-primary text-xs" disabled={controlling} onClick={() => playReview(item.media_id)}>
                                Play staged rip for review
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
                              <button type="button" className="btn btn-primary text-xs" disabled={controlling || !renameDrafts[item.media_id]?.trim()} onClick={() => saveManualEpisodeIdentification(item)}>
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
                            <button type="button" className="btn btn-secondary text-xs" onClick={onOpenDashboard}>Open this disc on the Disc Dashboard</button>
                          )}
                          <button type="button" className="btn btn-secondary text-xs" disabled={controlling} onClick={() => dismissPipelineItems([item.media_id])}>Leave out of active queue</button>
                          <button type="button" className="btn text-xs border border-red-400/50 bg-red-500/15 text-red-100 hover:bg-red-500/25" disabled={controlling} onClick={() => deleteQueuedStagedSource(item)}>Delete staged rip permanently</button>
                        </div>
                      </div>
                    ) : item.review_code === 'play_all_aggregate_detected' ? (
                      <div className="mt-2">This unmatched file closely matches the combined runtime and size of already matched contiguous episodes. It is being preserved as a likely play-all aggregate and is excluded from the missing-episode count.</div>
                    ) : ['special_feature_evidence_required', 'gemini_evidence_required', 'gemini_analysis_running', 'gemini_analysis_failed', 'gemini_audio_evidence_insufficient', 'gemini_catalog_unavailable', 'gemini_provider_failed', 'gemini_credential_rejected', 'gemini_rate_limited', 'gemini_provider_unavailable', 'gemini_request_rejected', 'gemini_network_failed', 'gemini_response_invalid', 'gemini_series_resolution_uncertain', 'gemini_descriptive_review_required', 'special_feature_manual_assignment_required'].includes(item.review_code || '') ? (
                      <div className="max-w-md rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-100">
                        <div>This title still needs a confident identification. It remains held; unrelated titles can continue through the queue.</div>
                        {item.review_code === 'gemini_evidence_required' && <div className="mt-2">Gemini fallback selected. Local evidence must be prepared first; selecting this did not contact Gemini or start HandBrake.</div>}
                        {item.review_code === 'gemini_analysis_running' && <div className="mt-2">Collecting bounded local audio evidence and running the confirmed Gemini comparison. It will requeue identification automatically when a confident allowed match is returned.</div>}
                        {item.review_code === 'gemini_analysis_failed' && <div className="mt-2">The evidence or Gemini request failed safely. No title was guessed; you may retry or choose a name manually.</div>}
                        {item.review_code === 'gemini_audio_evidence_insufficient' && <div className="mt-2">Local transcription did not produce enough usable dialogue. No Gemini request or title guess was made.</div>}
                        {item.review_code === 'gemini_catalog_unavailable' && <div className="mt-2">No reviewed bonus-feature catalogue matched this disc. Retry now uses bounded local evidence and catalogue-free Gemini classification to propose a provisional movie or extra name.</div>}
                        {item.review_code === 'gemini_descriptive_review_required' && (
                          <div className="mt-2">
                            {item.identification_attempts?.some((attempt) => attempt.branch === 'tv-bonus')
                              ? 'Gemini reviewed this as possible TV-disc bonus content but could not assign one safe descriptive name. Episode matching was already attempted first.'
                              : 'Gemini reviewed the bounded evidence but could not assign one safe movie or bonus-feature name.'}
                          </div>
                        )}
                        {item.review_code === 'gemini_provider_failed' && <div className="mt-2">The Gemini provider request failed safely. Check its credential status and network availability before retrying.</div>}
                        {item.review_code === 'gemini_credential_rejected' && <div className="mt-2">Gemini rejected or could not use the configured credentials. Check the key identifiers in Settings and rotate the rejected key.</div>}
                        {item.review_code === 'gemini_rate_limited' && <div className="mt-2">Gemini returned a quota or rate-limit response after bounded retries. Wait for quota recovery or check billing.</div>}
                        {item.review_code === 'gemini_provider_unavailable' && <div className="mt-2">Gemini returned a server error after bounded retries. Retry after the provider recovers.</div>}
                        {item.review_code === 'gemini_request_rejected' && <div className="mt-2">Gemini rejected the model/request combination. Check model availability and request compatibility.</div>}
                        {item.review_code === 'gemini_network_failed' && <div className="mt-2">RipWeaver could not reach Gemini after bounded retries. Check DNS, firewall, proxy, and internet access.</div>}
                        {item.review_code === 'gemini_response_invalid' && <div className="mt-2">Gemini responded, but its structured episode assignments did not pass validation.</div>}
                        {item.review_code === 'gemini_series_resolution_uncertain' && <div className="mt-2">Gemini could not resolve the disc label to one TMDb-validated television series with sufficient confidence. Confirm the canonical series name on the Disc Dashboard and retry.</div>}
                        {item.review_code === 'gemini_analysis_running' && geminiProgress[item.media_id] && <div className="mt-2 rounded border border-blue-400/30 bg-blue-400/10 p-2 text-blue-100">{geminiProgress[item.media_id]}</div>}
                        {item.review_code === 'special_feature_manual_assignment_required' && <div className="mt-2">Manual feature-name assignment selected.</div>}
                        <div className="mt-3 flex flex-wrap gap-2">
                          {item.review_code === 'gemini_descriptive_review_required' && (
                            <button type="button" className="btn btn-primary text-xs" disabled={controlling} onClick={() => analyzeHeldItemAsTv(item)}>Analyze as TV series</button>
                          )}
                          <button type="button" className="btn btn-secondary text-xs" disabled={controlling || item.review_code === 'gemini_analysis_running'} onClick={() => chooseAmbiguityResolution(item.media_id, 'gemini')}>{item.review_code === 'gemini_descriptive_review_required' ? 'Retry bonus-feature analysis' : item.review_code?.includes('failed') || item.review_code?.startsWith('gemini_') ? 'Retry local evidence and Gemini' : 'Use Gemini after local evidence'}</button>
                          <button type="button" className="btn btn-secondary text-xs" disabled={controlling} onClick={() => chooseAmbiguityResolution(item.media_id, 'manual')}>Choose name manually</button>
                          <button type="button" className="btn btn-secondary text-xs" disabled={controlling} onClick={() => chooseAmbiguityResolution(item.media_id, 'hold')}>Leave on hold</button>
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
                          <button type="button" className="btn btn-primary text-xs" disabled={controlling} onClick={() => playReview(item.media_id)}>
                            Play staged rip for review
                          </button>
                        )}
                        {item.staged_source_available && item.error_type === 'HandBrakeNoUsableAudio' && (
                          <button
                            type="button"
                            className="btn btn-secondary inline-flex min-w-56 items-center justify-center gap-2 text-xs"
                            disabled={controlling}
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
                        Reuse verified titles or resume a failed rip
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
                    <button className="btn btn-primary" disabled={controlling || !confirmPhysicalRip} onClick={() => controlJob('execute')}>
                      {preserveFailedPartials
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
