import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';

interface PreviewDrive {
  disc_id: string;
  drive_index: number;
  strategy: 'single-open' | 'per-title';
  title_count: number;
  estimated_bytes: number;
  minimum_length_seconds: number | null;
  reason: string;
  selection_mode: 'episode' | 'bonus-features' | 'mixed' | 'automatic-bonus-fallback' | 'reviewed-special-features';
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
  identification_status?: 'catalogue-match' | 'evidence-required' | null;
  prior_outcome_name?: string | null;
  prior_library_relative?: string | null;
  prior_episode_id?: string | null;
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
  collision_count: number;
  requires_review: boolean;
  limitations: string[];
}

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
  rip_progress_percent?: number | null;
  rip_transfer_mib_s?: number | null;
  rip_progress_scope?: string | null;
  rip_progress_updated_at?: string | null;
}

interface JobDashboard {
  automatic_processing_enabled: boolean;
  watcher_attached: boolean;
  jobs: OrchestrationJob[];
}

interface DriveSlot {
  drive_index: number;
  available: boolean;
  has_disc: boolean;
  disc_label: string | null;
  current_job_id?: string | null;
  current_disc_fingerprint?: string | null;
}

interface DriveDashboard {
  watcher_attached: boolean;
  refresh_mode: 'explicit';
  status: 'not_scanned' | 'ready' | 'error';
  refreshed_at: string | null;
  error_type: string | null;
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
  automatic_gemini_ambiguity_fallback?: boolean;
  jellyfin_tv_root?: string;
  jellyfin_movie_root?: string;
}

interface StoredProfile {
  profile_id: string;
  display_name: string;
  built_in: boolean;
}

interface PipelineQueueItem {
  media_id: string;
  artifact_sha256: string;
  disc_fingerprint: string | null;
  display_name: string | null;
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
  identification_attempts?: Array<{
    branch: string;
    disposition: string;
    summary: Record<string, string | number | boolean | null>;
  }>;
}

interface PipelineQueue {
  paused: boolean;
  downstream_worker_limit: number;
  items: PipelineQueueItem[];
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
  if (item.state === 'review_required') return item.review_code === 'all_season_analysis_running' ? 'Running all-season matching' : 'Review required';
  if (item.state === 'failed') return 'Stopped after an error';
  if (item.state === 'completed') return 'Completed';
  return `${item.stage} · ${item.state.replaceAll('_', ' ')}`;
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
  const [ejectingDrives, setEjectingDrives] = useState<number[]>([]);
  const [queuedEjectDrives, setQueuedEjectDrives] = useState<number[]>([]);
  const ejectQueueRef = useRef<Promise<void>>(Promise.resolve());
  const [renameDrafts, setRenameDrafts] = useState<Record<string, string>>({});
  const [confirmPhysicalRip, setConfirmPhysicalRip] = useState(false);
  const [preserveFailedPartials, setPreserveFailedPartials] = useState(false);
  const [failedCleanupPlan, setFailedCleanupPlan] = useState<FailedRipCleanupPlan | null>(null);
  const [pipelineQueue, setPipelineQueue] = useState<PipelineQueue | null>(null);
  const [transcodePlan, setTranscodePlan] = useState<TranscodeAuthorizationPlan | null>(null);
  const [organizationPlan, setOrganizationPlan] = useState<OrganizationAuthorizationPlan | null>(null);
  const [transcodeProfile, setTranscodeProfile] = useState('');
  const [jobDashboard, setJobDashboard] = useState<JobDashboard | null>(null);
  const [defaultProfile, setDefaultProfile] = useState('Default');
  const [rememberLastProfile, setRememberLastProfile] = useState(true);
  const [discSetups, setDiscSetups] = useState<Record<string, DiscSetup>>({});
  const [driveDashboard, setDriveDashboard] = useState<DriveDashboard | null>(null);
  const [refreshingDrives, setRefreshingDrives] = useState(false);
  const [preparingDrive, setPreparingDrive] = useState<number | null>(null);
  const [queuedPrepareDrives, setQueuedPrepareDrives] = useState<number[]>([]);
  const prepareQueue = useRef<Promise<void>>(Promise.resolve());
  const [handbrakeProfiles, setHandbrakeProfiles] = useState<StoredProfile[]>([]);
  const [automaticGeminiFallback, setAutomaticGeminiFallback] = useState(false);
  const [jellyfinRoots, setJellyfinRoots] = useState({ tv: '', movie: '' });
  const [unmatchedSeriesName, setUnmatchedSeriesName] = useState('');

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
      const [pipelineResponse, jobsResponse] = await Promise.all([
        fetch('/rip/pipeline/items'), fetch('/rip/jobs'),
      ]);
      if (pipelineResponse.ok) setPipelineQueue(await pipelineResponse.json());
      if (jobsResponse.ok) setJobDashboard(await jobsResponse.json());
      const drivesResponse = await fetch('/rip/drives');
      if (drivesResponse.ok) setDriveDashboard(await drivesResponse.json());
    };
    void refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => window.clearInterval(timer);
  }, []);

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
    const loadConfig = async () => {
      const response = await fetch('/system/config');
      if (!response.ok) return;
      const config = await response.json() as PublicConfig;
      if (config.default_handbrake_profile?.trim()) {
        setDefaultProfile(config.default_handbrake_profile.trim());
        setTranscodeProfile('auto-resolution');
      }
      setRememberLastProfile(config.remember_last_handbrake_profile !== false);
      setAutomaticGeminiFallback(Boolean(config.automatic_gemini_ambiguity_fallback));
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

  const driveSetupKey = (drive: DriveSlot) => `drive-${drive.drive_index}-${drive.disc_label || 'loaded'}`;

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

  const refreshDrives = async () => {
    setRefreshingDrives(true);
    setError('');
    try {
      const response = await fetch('/rip/drives/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm_read: true, timeout_seconds: 30 }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Drive refresh failed safely.');
      setDriveDashboard(payload as unknown as DriveDashboard);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Drive refresh failed safely.');
    } finally {
      setRefreshingDrives(false);
    }
  };

  const ejectDrive = async (drive: DriveSlot, alreadyConfirmed = false): Promise<boolean> => {
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
        window.alert(`Optical drive ${drive.drive_index + 1} was not ejected. ${message}`);
        return false;
      } finally {
        setEjectingDrives((current) => current.filter((index) => index !== drive.drive_index));
      }
    });
    ejectQueueRef.current = operation.then(() => undefined, () => undefined);
    return operation;
  };

  const cancelRipPlanAndEject = async () => {
    if (!savedJob || !preview) return;
    const drive = driveDashboard?.drives.find((item) => item.drive_index === preview.drives[0]?.drive_index);
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
    if (!window.confirm(`Permanently delete the verified staged rip for “${title}”? This does not change Jellyfin. It cannot be undone, and reripping the disc will be required to recover it.`)) return;
    setControlling(true);
    setError('');
    try {
      const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(item.media_id)}/delete-staged-source`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm_delete: true }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'The staged rip could not be deleted safely.');
      if (!Array.isArray(payload.items) || typeof payload.paused !== 'boolean') {
        throw new Error('The staged rip was handled, but the refreshed queue response was invalid. Refresh the page to confirm its state.');
      }
      setPipelineQueue(payload as unknown as PipelineQueue);
      setReviewNotice(`Permanently deleted the staged rip for “${title}” and removed it from the active queue. Jellyfin was not changed.`);
      window.alert(`Deleted the staged rip for “${title}”. Reripping will be required to recover it. Jellyfin was not changed.`);
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

  const saveManualEpisodeIdentification = async (item: PipelineQueueItem) => {
    const newName = (renameDrafts[item.media_id] || '').trim();
    if (!newName) return;
    if (!window.confirm(`Use "${newName}.mkv" as the reviewed identity for this staged rip? RipWeaver will preserve the original staged file, remember this title for the disc fingerprint, and continue the pipeline.`)) return;
    setControlling(true);
    setError('');
    try {
      const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(item.media_id)}/manual-episode-identification`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_name: newName, confirm_identification: true }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Manual episode identification could not be saved.');
      setRenameDrafts((current) => ({ ...current, [item.media_id]: '' }));
      const refreshed = await fetch('/rip/pipeline/items');
      if (refreshed.ok) setPipelineQueue(await refreshed.json());
      setReviewNotice('Saved the reviewed episode identity and returned the staged rip to the automatic pipeline. The .mkv extension is preserved.');
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

  const retryDriveJob = async (job: OrchestrationJob) => {
    const warning = 'Retry will preserve every partial and verified output. Unfinished titles restart from the beginning in a new run directory; nothing is overwritten. Continue?';
    if (!window.confirm(warning)) return;
    setError('');
    const response = await fetch(`/rip/jobs/${job.job_id}/retry`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': `retry-${crypto.randomUUID()}`,
      },
      body: JSON.stringify({ confirm_control: true }),
    });
    const payload = await response.json();
    if (!response.ok) {
      setError(typeof payload.detail === 'string' ? payload.detail : 'Rip retry could not be queued safely.');
      return;
    }
    setSavedJob(payload);
    if (payload.preview) setPreview(payload.preview);
    setConfirmPhysicalRip(false);
    const jobsResponse = await fetch('/rip/jobs');
    if (jobsResponse.ok) setJobDashboard(await jobsResponse.json());
  };

  const resumeInterruptedRip = async (job: OrchestrationJob) => {
    const titleCount = job.preview?.jobs.length ?? 0;
    if (!job.preview || titleCount === 0) {
      setError('The interrupted rip no longer has an exact reviewed title plan.');
      return;
    }
    setControlling(true);
    setError('');
    try {
      const cleanupResponse = await fetch(`/rip/jobs/${job.job_id}/failed-attempts`);
      const cleanup = await cleanupResponse.json();
      if (!cleanupResponse.ok) throw new Error(typeof cleanup.detail === 'string' ? cleanup.detail : 'Interrupted output could not be reviewed safely.');
      setFailedCleanupPlan(cleanup);
      const cleanupRequired = cleanup.attempt_directory_count > 0;
      const cleanupDescription = cleanupRequired
        ? ` permanently remove ${cleanup.attempt_directory_count} exact interrupted attempt folder(s) containing ${cleanup.file_count} incomplete MKV file(s) (${formatBytes(cleanup.total_bytes)}), then`
        : '';
      if (!window.confirm(
        `Resume this interrupted ${titleCount}-title rip? RipWeaver will${cleanupDescription} restart the reviewed titles in a new isolated attempt. Verified outputs and Jellyfin files will not be removed or overwritten.`,
      )) return;
      if (job.state === 'paused') {
        const resumeResponse = await fetch(`/rip/jobs/${job.job_id}/resume`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': `resume-${crypto.randomUUID()}`,
          },
          body: JSON.stringify({ confirm_control: true }),
        });
        const resumed = await resumeResponse.json();
        if (!resumeResponse.ok) throw new Error(typeof resumed.detail === 'string' ? resumed.detail : 'Interrupted rip could not be resumed safely.');
        setSavedJob(resumed);
        setPreview(resumed.preview);
      }

      const executeResponse = await fetch(`/rip/jobs/${job.job_id}/execute`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': `execute-resume-${crypto.randomUUID()}`,
        },
        body: JSON.stringify({
          expected_plan_sha256: job.plan_sha256,
          authorized_job_count: titleCount,
          timeout_seconds: 7200,
          max_drives: job.preview.drives.length,
          confirm_execute: true,
          preserve_failed_partials: !cleanupRequired,
          failed_cleanup_sha256: cleanupRequired ? cleanup.plan_sha256 : null,
          confirm_failed_cleanup: cleanupRequired,
        }),
      });
      const executed = await executeResponse.json();
      if (!executeResponse.ok) throw new Error(typeof executed.detail === 'string' ? executed.detail : 'Interrupted rip could not be restarted safely.');
      setSavedJob(executed);
      setPreview(executed.preview);
      setReviewNotice(cleanupRequired
        ? `Removed ${cleanup.attempt_directory_count} confirmed interrupted attempt folder(s) and restarted the rip. Verified and Jellyfin files were preserved.`
        : 'The interrupted rip restarted in a new isolated attempt. No incomplete MKV files required cleanup.');
      const jobsResponse = await fetch('/rip/jobs');
      if (jobsResponse.ok) setJobDashboard(await jobsResponse.json());
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Interrupted rip recovery failed safely.');
    } finally {
      setControlling(false);
    }
  };

  const runPreparedDrive = async (drive: DriveSlot, setup: DiscSetup) => {
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
          confirm_read: true,
          timeout_seconds: 300,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Disc pipeline could not be prepared safely.');
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

  const approveAndQueueJob = async () => {
    if (!savedJob) return;
    setControlling(true);
    setError('');
    try {
      const authorizeResponse = await fetch(`/rip/jobs/${savedJob.job_id}/authorize`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': `authorize-${crypto.randomUUID()}`,
        },
        body: JSON.stringify({
          expected_plan_sha256: savedJob.plan_sha256,
          confirm_authorization: true,
        }),
      });
      const authorized = await authorizeResponse.json();
      if (!authorizeResponse.ok) {
        throw new Error(typeof authorized.detail === 'string' ? authorized.detail : 'The title plan could not be approved safely.');
      }
      const queueResponse = await fetch(`/rip/jobs/${savedJob.job_id}/start`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': `start-${crypto.randomUUID()}`,
        },
        body: JSON.stringify({ confirm_queue: true }),
      });
      const queued = await queueResponse.json();
      if (!queueResponse.ok) {
        setSavedJob(authorized);
        throw new Error(typeof queued.detail === 'string' ? queued.detail : 'The approved plan could not be added to the queue safely.');
      }
      setSavedJob(queued);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'The title plan could not be queued safely.');
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
        'all_season_sequence_review_required',
      ].includes(item.review_code || '')),
    );
    const confirmation = allSeasonReady
      ? `Restart matching for ${scope} and automatically analyze the complete disc as “${suggestedUnmatchedSeries}”? This reads short MKV audio samples locally and queries episode metadata.${automaticGeminiFallback ? ' If local results remain ambiguous, the configured Gemini fallback may receive bounded evidence and candidate episode metadata.' : ''} It does not read the optical disc, rerip, rename, overwrite, delete, or transcode media.`
      : `Restart identification for verified existing rips covering ${scope}? This does not read the optical disc, rerip, rename, overwrite, or delete media. Files without a durable verified-rip record will remain held for verification.`;
    if (!window.confirm(confirmation)) return;
    setControlling(true);
    setError('');
    setReviewNotice('Checking durable verification records…');
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
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Existing rips could not be restarted safely.');
      const queueResponse = await fetch('/rip/pipeline/items');
      if (queueResponse.ok) setPipelineQueue(await queueResponse.json());
      setReviewNotice(`Restarted matching for ${payload.restarted_count} verified title(s). ${payload.verification_required_count} title(s) still require verification.`);
      setExistingRipsRestarted(payload.restarted_count > 0);
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : 'Existing rips could not be restarted safely.';
      setReviewNotice(message);
      setError(message);
      if (message.includes('require verification') && savedJob) {
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
    if (!window.confirm(`Run read-only FFprobe verification on ${candidateIds.length} exact staged MKV(s), then queue successful files for matching? No file will be changed, renamed, moved, or deleted.`)) return;
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
      setReviewNotice(`Verified ${payload.verified_count} existing MKV(s) and queued them for identification.`);
      setExistingRipsRestarted(payload.verified_count > 0);
      setExistingRecoveryPlan(null);
      const queueResponse = await fetch('/rip/pipeline/items');
      if (queueResponse.ok) setPipelineQueue(await queueResponse.json());
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
    const currentJobId = driveDashboard?.drives.find((drive) => drive.drive_index === driveIndex)?.current_job_id;
    if (!currentJobId) return undefined;
    return (jobDashboard?.jobs ?? []).find((candidate) => candidate.job_id === currentJobId);
  }, [driveDashboard?.drives, jobDashboard?.jobs]);

  useEffect(() => {
    const selectedDriveIndex = savedJob?.preview?.drives[0]?.drive_index;
    if (selectedDriveIndex === undefined || !driveDashboard?.drives.some((drive) => drive.drive_index === selectedDriveIndex && drive.has_disc)) return;
    const latest = latestJobForDrive(selectedDriveIndex);
    if (latest?.preview && latest.job_id !== savedJob?.job_id) {
      setSavedJob(latest);
      setPreview(latest.preview);
      setReviewNotice('The disc in this drive changed. Showing its newest saved review instead of the earlier disc review.');
      setError('');
    }
  }, [driveDashboard?.drives, latestJobForDrive, savedJob?.job_id, savedJob?.preview?.drives]);

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
  const selectedDriveSlot = driveDashboard?.drives.find(
    (drive) => drive.drive_index === preview?.drives[0]?.drive_index,
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
  const visiblePipelineItems = latestPipelineItems.filter((item) => !['completed', 'discarded'].includes(item.state));
  const visibleRipJobs = (jobDashboard?.jobs ?? []).filter((job) => {
    if (!['queued', 'running', 'pause_requested'].includes(job.state)) return false;
    if (queueOnly) return true;
    return Boolean(savedJob?.job_id && job.job_id === savedJob.job_id);
  });
  const existingRipsNeedAnalysis = visiblePipelineItems.some((item) =>
    item.stage === 'identify'
    && item.state === 'review_required'
    && ['missing_season_context', 'unmatched_disc_analysis_required', 'all_season_analysis_failed', 'all_season_sequence_review_required'].includes(item.review_code || '')
  );
  const existingRipsInPipeline = existingRipsRestarted || visiblePipelineItems.some(
    (item) => item.disc_fingerprint === selectedDiscFingerprint && item.media_id.includes('-recovery-'),
  );
  const currentTitleOutcome = (titleIndex: number) => latestPipelineItems.find((item) => {
    const match = item.media_id.match(/-title-(\d{3})(?:-|$)/);
    return match !== null && Number(match[1]) === titleIndex;
  });
  const selectedDriveLabel = driveDashboard?.drives.find(
    (drive) => drive.drive_index === preview?.drives[0]?.drive_index
  )?.disc_label || '';
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
    .replace(/\bSEASON\s*\d+\b.*$/i, '')
    .replace(/\s+\d+\s*$/i, '')
    .replace(/\s+/g, ' ')
    .trim();
  const explicitSeasonMatch = selectedDriveLabel.match(/\bSEASON\s*(\d{1,2})\b/i);
  const suggestedSeason = explicitSeasonMatch ? Number(explicitSeasonMatch[1]) : null;
  const runAllSeasonAnalysis = async () => {
    if (!selectedDiscFingerprint) return;
    const reviewedSeries = unmatchedSeriesName.trim() || suggestedUnmatchedSeries;
    if (!reviewedSeries) {
      setReviewNotice('Enter the canonical TV series name before starting all-season analysis.');
      return;
    }
    const scopeLabel = suggestedSeason === null ? 'across every aired season' : `against Season ${suggestedSeason}`;
    if (!window.confirm(`Analyze the held MKVs as “${reviewedSeries}” ${scopeLabel}? This reads short audio samples, transcribes them locally, and queries TMDb episode metadata.${automaticGeminiFallback ? ' If local matching remains ambiguous, the configured Gemini fallback may receive bounded transcript excerpts and candidate episode metadata.' : ' Gemini fallback is disabled in Settings.'} It does not rename, move, delete, or transcode media.`)) return;
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
          confirm_external_fallback: automaticGeminiFallback,
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

  return (
    <div className="h-full overflow-auto space-y-6">
      <div className={queueOnly || attentionOnly ? 'hidden' : 'contents'}>
      <div>
        <h2 className="text-3xl font-bold heading-gradient mb-1">Disc Dashboard</h2>
        <p className="text-sm text-[var(--text-muted)]">
          See every detected optical drive, configure each inserted disc, and follow it through the pipeline.
        </p>
      </div>

      {jobDashboard && (
        <div className="glass-panel rounded-xl p-5 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="font-bold text-white">Optical drives</div>
              <div className="text-sm text-[var(--text-muted)]">
                {jobDashboard.automatic_processing_enabled ? 'Automatic processing requested' : 'Automatic processing disabled'} · {driveDashboard?.status === 'ready' ? 'drive status refreshed' : 'waiting for read-only refresh'}
              </div>
            </div>
            <button type="button" className="btn btn-secondary" onClick={refreshDrives} disabled={refreshingDrives}>
              {refreshingDrives ? 'Reading drive slots…' : 'Refresh drives (read-only)'}
            </button>
          </div>
          {driveDashboard?.status !== 'ready' && (
            <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-100">
              Select “Refresh drives” to perform one read-only MakeMKV slot discovery. It enumerates loaded and empty trays but does not inventory titles or start ripping.
            </div>
          )}
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
            {!driveDashboard || driveDashboard.drives.length === 0 ? (
              <div className="rounded-xl border border-dashed border-[var(--border-color)] p-8 text-center">
                <div className="text-5xl opacity-40 mb-3">▱</div>
                <div className="font-semibold text-white">No detected drive data</div>
                <div className="text-sm text-[var(--text-muted)] mt-1">Refresh to show every empty or loaded optical-drive slot.</div>
              </div>
            ) : driveDashboard.drives.map((drive) => {
              const driveKey = driveSetupKey(drive);
              const setup = getDiscSetup(driveKey);
              const job = latestJobForDrive(drive.drive_index);
              const driveJobs = (jobDashboard?.jobs ?? []).filter((candidate) => candidate.preview?.drives.some((item) => item.drive_index === drive.drive_index));
              const earlierActiveJob = driveJobs.find((candidate) => candidate.job_id !== job?.job_id && ['authorized', 'queued', 'running', 'pause_requested'].includes(candidate.state));
              const driveFingerprint = job?.preview?.jobs.map((item) => item.staging_destination.match(/(?:^|\/)([0-9a-f]{16})(?:\/|$)/)?.[1]).find((value): value is string => Boolean(value));
              const drivePipelineItems = (pipelineQueue?.items ?? []).filter((item) => driveFingerprint && item.disc_fingerprint === driveFingerprint);
              const identificationNeedsAttention = drivePipelineItems.some((item) => item.stage === 'identify' && ['failed', 'review_required'].includes(item.state));
              const discardedIdentificationRemains = drivePipelineItems.some((item) => item.stage === 'identify' && item.state === 'discarded' && item.staged_source_available);
              const driveFailed = job?.state === 'failed' && (job.failed_drive_indexes?.length === 0 || job.failed_drive_indexes?.includes(drive.drive_index));
              const driveNeedsReview = job?.state === 'awaiting_review';
              const drivePaused = job?.state === 'paused';
              const interruptedQueued = job?.state === 'queued' && job.executor_attached === false && job.rip_progress_percent !== null && job.rip_progress_percent !== undefined;
              const driveNeedsAction = driveNeedsReview || drivePaused || job?.state === 'queued' || identificationNeedsAttention || discardedIdentificationRemains || Boolean(earlierActiveJob);
              const driveStatus = identificationNeedsAttention
                ? 'identification needs attention'
                : discardedIdentificationRemains
                  ? 'unidentified rip preserved'
                  : earlierActiveJob
                    ? 'eject held by earlier rip job'
                    : job?.state === 'completed'
                      ? 'rip completed'
                      : job?.state.replaceAll('_', ' ') ?? 'disc inserted';
              const selectedDriveJob = savedJob?.job_id === job?.job_id;
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
                      <div className="font-semibold text-white">Optical drive {drive.drive_index + 1}{drive.disc_label ? ` — ${drive.disc_label}` : ''}</div>
                      <div className={`text-xs font-bold uppercase ${driveNeedsAction || driveFailed ? 'text-red-300' : drive.has_disc ? 'text-blue-300' : 'text-slate-400'}`}>{drive.has_disc ? driveStatus : 'empty tray'}</div>
                    </div>
                  </div>
                  {drive.has_disc && <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <label className="space-y-1">
                      <span className="text-xs font-semibold text-white">Disc contains</span>
                      <select className="w-full rounded-lg bg-[var(--bg-primary)] border border-[var(--border-color)] p-2 text-sm text-white" value={setup.contentType} onChange={(event) => updateDiscSetup(driveKey, { contentType: event.target.value as DiscContentType })}>
                        <option value="">Automatic (no hint)</option>
                        <option value="tv">TV episodes</option>
                        <option value="movie">Movie</option>
                        <option value="extras">Extras / bonus disc</option>
                        <option value="mixed">Mixed main titles + extras</option>
                      </select>
                    </label>
                    <label className="space-y-1">
                      <span className="text-xs font-semibold text-white">HandBrake profile</span>
                      <select className="w-full rounded-lg bg-[var(--bg-primary)] border border-[var(--border-color)] p-2 text-sm text-white" value={setup.handbrakeProfile} onChange={(event) => { const profileId = event.target.value; if (profileId === '__custom__') { updateDiscSetup(driveKey, { handbrakeProfile: '' }); onOpenSettings?.(); return; } updateDiscSetup(driveKey, { handbrakeProfile: profileId }); void rememberDefaultProfile(profileId); }}>
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
                        <span className="font-semibold">MakeMKV ripping</span>
                        <span>{job.rip_progress_percent ?? 0}%{job.rip_transfer_mib_s ? ` · ${job.rip_transfer_mib_s.toFixed(2)} MiB/s` : ''}</span>
                      </div>
                      <div className="mt-2 h-2 overflow-hidden rounded bg-black/30">
                        <div className="h-full bg-blue-400 transition-all" style={{ width: `${job.rip_progress_percent ?? 0}%` }} />
                      </div>
                      <div className="mt-1 text-xs text-blue-100/70">{job.rip_progress_scope && job.rip_progress_scope !== 'batch' ? `${job.rip_progress_scope} · ` : ''}rate appears after two MakeMKV progress samples.</div>
                      {job.rip_progress_updated_at && Date.now() - Date.parse(job.rip_progress_updated_at) > 120000 && (
                        <div className="mt-2 rounded border border-amber-400/40 bg-amber-500/15 p-2 text-xs text-amber-100">
                          No measurable progress for over two minutes. MakeMKV may be retrying a difficult read. If it does not recover, stop safely, clean the disc, or try another drive.
                        </div>
                      )}
                    </div>
                  )}
                  {drive.has_disc && identificationNeedsAttention && (
                    <div className="rounded-lg border border-amber-400/40 bg-amber-500/15 p-3 text-sm text-amber-100 space-y-2">
                      <div>Ripping finished, but one or more titles still need identification review. The disc pipeline is not fully complete.</div>
                      <button
                        type="button"
                        className="btn btn-secondary text-xs"
                        disabled={preparingDrive === drive.drive_index || queuedPrepareDrives.includes(drive.drive_index)}
                        onClick={() => queueDrivePipeline(drive, setup)}
                      >
                        {preparingDrive === drive.drive_index
                          ? 'Preparing fresh rerip plan…'
                          : queuedPrepareDrives.includes(drive.drive_index)
                            ? 'Fresh rerip preparation queued'
                            : 'Prepare a fresh rerip plan'}
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
                  {drive.available && (
                    <button type="button" className="btn btn-secondary" disabled={ejectingDrives.includes(drive.drive_index) || queuedEjectDrives.includes(drive.drive_index) || ['authorized', 'queued', 'running', 'pause_requested'].includes(job?.state || '')} onClick={() => ejectDrive(drive)}>
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
                  {drive.has_disc && !identificationNeedsAttention && !discardedIdentificationRemains && (!job || ['completed', 'failed'].includes(job.state) || stagingAttemptCollision) && (
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
                  {drive.has_disc && job?.preview && (['awaiting_review', 'authorized', 'queued', 'failed'].includes(job.state) || identificationNeedsAttention || discardedIdentificationRemains) && (
                    <button type="button" className={driveNeedsReview || driveFailed ? 'btn btn-primary w-full' : 'btn btn-secondary w-full'} onClick={() => selectDriveJob(job)}>
                      {selectedDriveJob ? 'This disc is shown below' : driveNeedsReview ? 'Review this disc' : driveFailed ? 'Review error and retry options' : 'Open this disc’s controls'}
                    </button>
                  )}
                  {drive.has_disc && driveFailed && job && (
                    <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 space-y-2">
                      <div className="font-semibold text-red-200">Rip stopped safely{job.error_category ? ` · ${job.error_category.replaceAll('_', ' ')}` : ''}</div>
                      <div className="text-xs text-red-100/80">Partials were preserved. A retry never writes into an incomplete MKV and will use a new run directory.</div>
                      {(job.recommendations ?? []).map((recommendation) => <div key={recommendation} className="text-xs text-amber-100">• {recommendation}</div>)}
                      <button type="button" className="btn btn-secondary" onClick={() => void retryDriveJob(job)}>Retry unfinished titles</button>
                    </div>
                  )}
                  {drive.has_disc && (drivePaused || interruptedQueued) && job && (
                    <div className="rounded-lg border border-amber-400/40 bg-amber-500/15 p-3 space-y-2">
                      <div className="font-semibold text-amber-100">Interrupted rip needs recovery</div>
                      <div className="text-xs text-amber-100/80">Recovery removes only exact incomplete MKVs and empty title folders from the interrupted attempt, then starts the reviewed titles again. Verified outputs and Jellyfin files remain protected.</div>
                      <button type="button" className="btn btn-primary" disabled={controlling} onClick={() => void resumeInterruptedRip(job)}>Delete incomplete attempt and restart rip</button>
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
                  <div>{job.state === 'running' ? `MakeMKV is ripping now${job.rip_progress_percent !== null && job.rip_progress_percent !== undefined ? ` · ${job.rip_progress_percent}%` : ''}${job.rip_transfer_mib_s ? ` · ${job.rip_transfer_mib_s.toFixed(2)} MiB/s` : ''}.` : job.state === 'pause_requested' ? 'The rip will pause safely after active work settles.' : 'The reviewed rip is waiting to start.'}</div>
                  <div className="mt-1 text-xs text-blue-100/75">
                    {job.preview?.jobs.length ?? 0} reviewed title(s). Each title enters identification only after its MKV finishes and verifies, so the downstream list may remain unchanged during a long title.
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1">
                    {(job.preview?.jobs ?? []).map((ripItem) => <span key={`${job.job_id}-${ripItem.title_index}`} className="rounded border border-blue-300/30 px-2 py-1 text-xs">Title {ripItem.title_index} · rip pending/active</span>)}
                  </div>
                </div>
              ))}
            </div>
          )}
          {!queueOnly && !attentionOnly && visiblePipelineItems.some((item) => ['missing_season_context', 'unmatched_disc_analysis_required', 'all_season_analysis_failed', 'all_season_sequence_review_required'].includes(item.review_code || '')) && (
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
                {onOpenSettings && <button type="button" className="btn btn-secondary" onClick={onOpenSettings}>Repair HandBrakeCLI path in settings</button>}
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
              {visiblePipelineItems.some((item) => ['missing_season_context', 'unmatched_disc_analysis_required', 'all_season_analysis_failed', 'all_season_sequence_review_required'].includes(item.review_code || '')) && (
                <div>Episode identification needs a series/season analysis choice. Use the analysis panel above.</div>
              )}
              {visiblePipelineItems.some((item) => item.review_code === 'library_collision') && (
                <div>A matched Jellyfin destination already exists. Each affected card lets you encode/replace after verification or delete only the new pipeline copy.</div>
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
                  <div>Bonus-feature identification needs Gemini evidence, a manual name, or an explicit hold choice.</div>
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
            <div className="text-sm text-[var(--text-muted)]">{attentionOnly ? 'No pipeline errors or review choices need attention.' : queueOnly ? 'No actionable downstream work is waiting. Completed items are available in Recently Finished.' : savedJob?.state === 'queued' ? 'This disc has not produced verified rips yet. Complete the MakeMKV confirmation below; identification will enter this queue automatically after each rip verifies.' : selectedDiscFingerprint ? 'No actionable downstream work belongs to this selected disc. Completed items are available in Recently Finished.' : 'Select a disc to see only its downstream items.'}</div>
          ) : (
            <div id="pipeline-review-actions" className="divide-y divide-[var(--border-color)] scroll-mt-4">
              {visiblePipelineItems.map((item) => (
                <div key={item.media_id} className="py-3 flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <div className="font-mono text-sm text-white"><span className="mr-2">{stageIcon[item.state === 'completed' ? 'complete' : item.stage] || '⏸️'}</span>{item.media_id}</div>
                    <div className="text-sm font-semibold text-white">Matched title: {item.display_name || 'Not matched yet'}</div>
                    <div className="text-[11px] text-[var(--text-muted)]">The identifier above is the internal recovery ID.</div>
                    <div className={`text-xs font-semibold ${item.state === 'running' ? 'text-blue-200' : item.state === 'queued' ? 'text-amber-200' : item.state === 'failed' ? 'text-red-200' : 'text-[var(--text-muted)]'}`}>
                      {pipelineStatusLabel(item)}
                      {item.review_code ? ` · ${item.review_code}` : ''}
                      {item.error_type ? ` · ${item.error_type}` : ''}
                    </div>
                    {(item.identification_attempts?.length || 0) > 0 && (
                      <div className="mt-1 max-w-2xl text-[11px] text-[var(--text-muted)]">
                        Identification tried: {item.identification_attempts!.map((attempt) => `${attempt.branch} (${attempt.disposition})`).join(' → ')}
                      </div>
                    )}
                  </div>
                  <div className="flex gap-1 text-xs">
                    {pipelineStages.map((stage) => <span key={stage} className={`rounded border px-2 py-1 transition-colors ${pipelineStageClass(item, stage)}`}>{stageIcon[stage]} {stage}</span>)}
                  </div>
                  {item.state === 'queued' && (
                    <div className="flex flex-wrap gap-2">
                      <button type="button" className="btn btn-secondary text-xs" disabled={controlling} onClick={() => cancelQueuedPipelineItems([item.media_id])}>
                        Remove from queue — keep staged files
                      </button>
                      {item.stage === 'transcode' && (
                        <button type="button" className="btn text-xs border border-red-400/50 bg-red-500/15 text-red-100 hover:bg-red-500/25" disabled={controlling} onClick={() => deleteQueuedStagedSource(item)}>
                          Delete staged rip permanently
                        </button>
                      )}
                    </div>
                  )}
                  {item.stage === 'organize' && item.state === 'queued' && (
                    <div className="max-w-md rounded-lg border border-blue-400/30 bg-blue-400/10 p-3 text-xs text-blue-100">
                      Transcoding and verification finished. Waiting for Jellyfin destination and collision review; this file has not been moved into the library.
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
                    ) : ['missing_season_context', 'unmatched_disc_analysis_required', 'all_season_analysis_failed', 'all_season_sequence_review_required'].includes(item.review_code || '') ? (
                      <div className="max-w-md rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-100">
                        <div>This title needs episode-sequence analysis before it can continue.</div>
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
                                placeholder="Series - S03E02 - Episode Title"
                                aria-label="Reviewed filename without .mkv"
                              />
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
                    ) : ['special_feature_evidence_required', 'gemini_evidence_required', 'gemini_analysis_running', 'gemini_analysis_failed', 'gemini_audio_evidence_insufficient', 'gemini_catalog_unavailable', 'gemini_provider_failed', 'gemini_credential_rejected', 'gemini_rate_limited', 'gemini_provider_unavailable', 'gemini_request_rejected', 'gemini_network_failed', 'gemini_response_invalid', 'gemini_descriptive_review_required', 'special_feature_manual_assignment_required'].includes(item.review_code || '') ? (
                      <div className="max-w-md rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-100">
                        <div>This title has two or more plausible bonus-feature names. It remains held; unrelated titles can continue through the queue.</div>
                        {item.review_code === 'gemini_evidence_required' && <div className="mt-2">Gemini fallback selected. Local evidence must be prepared first; selecting this did not contact Gemini or start HandBrake.</div>}
                        {item.review_code === 'gemini_analysis_running' && <div className="mt-2">Collecting bounded local audio evidence and running the confirmed Gemini comparison. It will requeue identification automatically when a confident allowed match is returned.</div>}
                        {item.review_code === 'gemini_analysis_failed' && <div className="mt-2">The evidence or Gemini request failed safely. No title was guessed; you may retry or choose a name manually.</div>}
                        {item.review_code === 'gemini_audio_evidence_insufficient' && <div className="mt-2">Local transcription did not produce enough usable dialogue. No Gemini request or title guess was made.</div>}
                        {item.review_code === 'gemini_catalog_unavailable' && <div className="mt-2">No reviewed bonus-feature catalogue matched this disc. Retry now uses bounded local evidence and catalogue-free Gemini classification to propose a provisional movie or extra name.</div>}
                        {item.review_code === 'gemini_descriptive_review_required' && <div className="mt-2">Gemini reviewed this through the movie/bonus path but could not classify it safely. If this came from a TV disc, restart it against the aired episode catalogue instead.</div>}
                        {item.review_code === 'gemini_provider_failed' && <div className="mt-2">The Gemini provider request failed safely. Check its credential status and network availability before retrying.</div>}
                        {item.review_code === 'gemini_credential_rejected' && <div className="mt-2">Gemini rejected or could not use the configured credentials. Check the key identifiers in Settings and rotate the rejected key.</div>}
                        {item.review_code === 'gemini_rate_limited' && <div className="mt-2">Gemini returned a quota or rate-limit response after bounded retries. Wait for quota recovery or check billing.</div>}
                        {item.review_code === 'gemini_provider_unavailable' && <div className="mt-2">Gemini returned a server error after bounded retries. Retry after the provider recovers.</div>}
                        {item.review_code === 'gemini_request_rejected' && <div className="mt-2">Gemini rejected the model/request combination. Check model availability and request compatibility.</div>}
                        {item.review_code === 'gemini_network_failed' && <div className="mt-2">RipWeaver could not reach Gemini after bounded retries. Check DNS, firewall, proxy, and internet access.</div>}
                        {item.review_code === 'gemini_response_invalid' && <div className="mt-2">Gemini responded, but its structured episode assignments did not pass validation.</div>}
                        {item.review_code === 'gemini_analysis_running' && geminiProgress[item.media_id] && <div className="mt-2 rounded border border-blue-400/30 bg-blue-400/10 p-2 text-blue-100">{geminiProgress[item.media_id]}</div>}
                        {item.review_code === 'special_feature_manual_assignment_required' && <div className="mt-2">Manual feature-name assignment selected.</div>}
                        <div className="mt-3 flex flex-wrap gap-2">
                          {item.review_code === 'gemini_descriptive_review_required' && (
                            <button type="button" className="btn btn-primary text-xs" disabled={controlling} onClick={() => analyzeHeldItemAsTv(item)}>Analyze as TV series</button>
                          )}
                          <button type="button" className="btn btn-secondary text-xs" disabled={controlling || item.review_code === 'gemini_analysis_running'} onClick={() => chooseAmbiguityResolution(item.media_id, 'gemini')}>{item.review_code === 'gemini_descriptive_review_required' ? 'Retry as movie or bonus feature' : item.review_code?.includes('failed') || item.review_code?.startsWith('gemini_') ? 'Retry local evidence and Gemini' : 'Use Gemini after local evidence'}</button>
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
                        <button type="button" className="btn btn-secondary" onClick={() => controlPipeline('resume', item.media_id)}>
                          Retry item
                        </button>
                        <button type="button" className="btn btn-secondary" disabled={controlling} onClick={() => dismissPipelineItems([item.media_id])}>
                          Clear from queue
                        </button>
                        <button type="button" className="btn text-xs border border-red-400/50 bg-red-500/15 text-red-100 hover:bg-red-500/25" disabled={controlling} onClick={() => deleteQueuedStagedSource(item)}>
                          Delete staged rip permanently
                        </button>
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
              <div className="text-xl font-bold text-white">Reviewing optical drive {(preview.drives[0]?.drive_index ?? 0) + 1}</div>
            </div>
            <button type="button" className="btn btn-secondary" onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}>Back to drives</button>
          </div>
          <div className={`rounded-xl border p-4 ${preview.requires_review
            ? 'border-amber-500/30 bg-amber-500/10'
            : 'border-green-500/30 bg-green-500/10'
          }`}>
            <div className="font-bold text-white">
              {existingRipsNeedAnalysis ? 'Existing rips need episode analysis' : existingRipsInPipeline ? 'Existing rips are processing' : preview.requires_review ? 'Your attention is needed' : 'Ready for your approval'}
            </div>
            <div className="text-sm text-[var(--text-muted)] mt-1">
              {existingRipsNeedAnalysis
                ? `${preview.jobs.length} completed MKV(s) were accepted, but ordinary identification could not determine their episodes. Start the all-season matcher below.`
                : existingRipsInPipeline
                ? `${preview.jobs.length} existing completed MKV(s) were accepted as inputs. No rerip or overwrite decision is needed while identification proceeds.`
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

          {savedJob && (
            <div className="glass-panel rounded-xl p-5">
              {reviewNotice && (
                <div className="mb-4 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-100">
                  {reviewNotice}
                </div>
              )}
              {existingRecoveryPlan && (
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
                      Verify checked MKVs and restart matching
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
                    <div className="font-semibold text-blue-100">Using the existing completed MKVs</div>
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
                      <button type="button" className="btn btn-secondary" disabled={controlling} onClick={restartExistingPipeline}>
                        Use verified existing rips and restart matching
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
                      {driveDashboard?.drives.find((drive) => drive.drive_index === preview.drives[0]?.drive_index) && (
                        <button
                          type="button"
                          className="btn btn-secondary"
                          disabled={controlling || preparingDrive !== null}
                          onClick={() => {
                            const drive = driveDashboard.drives.find((item) => item.drive_index === preview.drives[0]?.drive_index);
                            if (drive) queueDrivePipeline(drive, getDiscSetup(driveSetupKey(drive)));
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
                        Use verified existing rips and restart matching
                      </button>
                      <button type="button" className="btn btn-primary text-left" disabled={controlling} onClick={() => resolveRipCollisions('missing-only')}>
                        Rip only missing titles
                      </button>
                      <button type="button" className="btn btn-secondary text-left" disabled={controlling} onClick={() => resolveRipCollisions('rerip-all')}>
                        Rip all titles again as replacement copies
                      </button>
                      <button type="button" className="btn btn-secondary text-left border-red-500/50 text-red-100" disabled={controlling} onClick={() => resolveRipCollisions('replace-after-verification')}>
                        Deliberately replace existing completed files
                      </button>
                    </div>
                    <div className="text-xs text-amber-100/70 mt-3">
                      “Missing only” excludes every title with an existing planned output or partial. “Replacement copies” uses new collision-safe folders. “Deliberately replace” records your intent, but still rerips safely first and requires an exact second confirmation after verification. Nothing is deleted at this rip stage.
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
                        {job.prior_library_relative && <div className="mt-2 break-all font-mono text-[11px] text-green-50">Jellyfin location: {job.prior_library_relative}</div>}
                      </div>
                    )}
                    {job.display_name && <div className="text-sm font-semibold text-white">{job.display_name}</div>}
                    {job.extras_folder && <div className="text-xs text-indigo-200">Jellyfin extras category: {job.extras_folder}</div>}
                    {job.identification_status === 'evidence-required' && (
                      <div className="text-xs text-amber-300">Name remains held for fingerprint, image, OCR, or audio evidence after ripping.</div>
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
                    {existingRipsInPipeline && job.collision_status === 'final-exists'
                      ? 'existing MKV accepted for matching'
                      : job.collision_status}
                  </div>
                </div>
              ))}
            </div>
          </div>

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
