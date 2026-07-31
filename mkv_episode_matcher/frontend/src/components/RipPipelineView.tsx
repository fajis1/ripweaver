import { useEffect, useState, type FormEvent } from 'react';

interface PreviewDrive {
  disc_id: string;
  drive_index: number;
  strategy: 'single-open' | 'per-title';
  title_count: number;
  estimated_bytes: number;
  minimum_length_seconds: number | null;
  reason: string;
}

interface PreviewJob {
  job_id: string;
  drive_index: number;
  title_index: number;
  estimated_bytes: number | null;
  staging_destination: string;
  final_destination: string | null;
  collision_status: string;
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
}

interface PipelineQueueItem {
  media_id: string;
  state: string;
  stage: string;
  error_type: string | null;
  review_code: string | null;
}

interface PipelineQueue {
  paused: boolean;
  downstream_worker_limit: number;
  items: PipelineQueueItem[];
}

const formatBytes = (value: number | null) => {
  if (!value) return 'Unknown';
  return `${(value / (1024 ** 3)).toFixed(2)} GiB`;
};

const RipPipelineView = () => {
  const [reportText, setReportText] = useState('');
  const [seriesName, setSeriesName] = useState('');
  const [season, setSeason] = useState('1');
  const [outputRoot, setOutputRoot] = useState('');
  const [preview, setPreview] = useState<RipPreview | null>(null);
  const [previewRequest, setPreviewRequest] = useState<PreviewRequest | null>(null);
  const [creationKey, setCreationKey] = useState('');
  const [savedJob, setSavedJob] = useState<OrchestrationJob | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [controlling, setControlling] = useState(false);
  const [makeMkvExecutable, setMakeMkvExecutable] = useState('');
  const [runDirectory, setRunDirectory] = useState('');
  const [confirmPhysicalRip, setConfirmPhysicalRip] = useState(false);
  const [pipelineQueue, setPipelineQueue] = useState<PipelineQueue | null>(null);

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
      const response = await fetch('/rip/pipeline/items');
      if (response.ok) setPipelineQueue(await response.json());
    };
    void refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => window.clearInterval(timer);
  }, []);

  const controlPipeline = async (action: 'pause' | 'resume', mediaId?: string) => {
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
    }
  };

  const controlJob = async (action: 'authorize' | 'start' | 'execute' | 'pause' | 'stop') => {
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
        if (!confirmPhysicalRip || !makeMkvExecutable.trim() || !runDirectory.trim() || !preview) {
          throw new Error('Confirm the physical rip and provide MakeMKV and a new run directory.');
        }
        body = {
          expected_plan_sha256: savedJob.plan_sha256,
          authorized_job_count: preview.jobs.length,
          makemkv_executable: makeMkvExecutable.trim(),
          run_directory: runDirectory.trim(),
          timeout_seconds: 7200,
          max_drives: preview.drives.length,
          confirm_execute: true,
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

  return (
    <div className="h-full overflow-auto space-y-6">
      <div>
        <h2 className="text-3xl font-bold heading-gradient mb-1">Disc Pipeline Preview</h2>
        <p className="text-sm text-[var(--text-muted)]">
          Inspect saved preflight reports, authorize the exact plan, and monitor an explicitly started rip.
        </p>
      </div>

      <form onSubmit={handlePreview} className="glass-panel rounded-2xl p-6 space-y-5">
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

      {error && (
        <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-red-200">
          {error}
        </div>
      )}

      {pipelineQueue && (
        <div className="glass-panel rounded-xl p-5 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="font-bold text-white">Downstream media queue</div>
              <div className="text-sm text-[var(--text-muted)]">
                One global worker shared by identification, fallback analysis, transcoding, and organization.
              </div>
            </div>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => controlPipeline(pipelineQueue.paused ? 'resume' : 'pause')}
            >
              {pipelineQueue.paused ? 'Resume downstream queue' : 'Pause downstream queue'}
            </button>
          </div>
          {pipelineQueue.items.length === 0 ? (
            <div className="text-sm text-[var(--text-muted)]">No verified rips are waiting.</div>
          ) : (
            <div className="divide-y divide-[var(--border-color)]">
              {pipelineQueue.items.map((item) => (
                <div key={item.media_id} className="py-3 flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <div className="font-mono text-sm text-white">{item.media_id}</div>
                    <div className="text-xs text-[var(--text-muted)]">
                      {item.stage} · {item.state.replaceAll('_', ' ')}
                      {item.review_code ? ` · ${item.review_code}` : ''}
                      {item.error_type ? ` · ${item.error_type}` : ''}
                    </div>
                  </div>
                  {['failed', 'review_required'].includes(item.state) && (
                    <button type="button" className="btn btn-secondary" onClick={() => controlPipeline('resume', item.media_id)}>
                      Retry item
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {preview && (
        <div className="space-y-5 animate-fade-in">
          <div className={`rounded-xl border p-4 ${preview.requires_review
            ? 'border-amber-500/30 bg-amber-500/10'
            : 'border-green-500/30 bg-green-500/10'
          }`}>
            <div className="font-bold text-white">
              {preview.requires_review ? 'Review required' : 'Preview is collision-free'}
            </div>
            <div className="text-sm text-[var(--text-muted)] mt-1">
              {preview.jobs.length} title(s), {preview.collision_count} collision(s). Execution remains disabled.
            </div>
            <div className="text-xs font-mono text-[var(--text-muted)] mt-2">
              Preview SHA-256: {preview.plan_sha256}
            </div>
            <div className="flex flex-wrap items-center justify-between gap-3 mt-4">
              <div className="text-xs text-[var(--text-muted)]">
                Saving creates a restart-safe review record. It does not authorize or start MakeMKV.
              </div>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={saveDurableJob}
                disabled={saving || savedJob !== null}
              >
                {savedJob ? 'Durable job saved' : saving ? 'Saving…' : 'Save job for review'}
              </button>
            </div>
          </div>

          {savedJob && (
            <div className="glass-panel rounded-xl p-5">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <div className="text-xs uppercase tracking-wider text-[var(--text-muted)]">Durable orchestration job</div>
                  <div className="font-mono text-sm text-white mt-1">{savedJob.job_id}</div>
                </div>
                <span className="px-3 py-1 rounded-full bg-blue-500/15 text-blue-300 text-xs font-bold uppercase">
                  {savedJob.state.replaceAll('_', ' ')}
                </span>
              </div>
              <div className="text-sm text-[var(--text-muted)] mt-3">
                Executor attached: {savedJob.executor_attached ? 'yes' : 'no'}.
              </div>
              <div className="mt-4 space-y-3">
                {savedJob.state === 'awaiting_review' && (
                  <button className="btn btn-secondary" disabled={controlling} onClick={() => controlJob('authorize')}>Authorize exact plan</button>
                )}
                {savedJob.state === 'authorized' && (
                  <button className="btn btn-secondary" disabled={controlling} onClick={() => controlJob('start')}>Queue authorized job</button>
                )}
                {savedJob.state === 'queued' && (
                  <div className="grid gap-3">
                    <input className="rounded-xl bg-[var(--bg-primary)] border border-[var(--border-color)] p-3 text-white" value={makeMkvExecutable} onChange={(event) => setMakeMkvExecutable(event.target.value)} placeholder="Exact makemkvcon executable path" />
                    <input className="rounded-xl bg-[var(--bg-primary)] border border-[var(--border-color)] p-3 text-white" value={runDirectory} onChange={(event) => setRunDirectory(event.target.value)} placeholder="New dedicated run/log directory" />
                    <label className="text-sm text-amber-200"><input type="checkbox" className="mr-2" checked={confirmPhysicalRip} onChange={(event) => setConfirmPhysicalRip(event.target.checked)} />I authorize this exact title set to be physically ripped.</label>
                    <button className="btn btn-primary" disabled={controlling || !confirmPhysicalRip} onClick={() => controlJob('execute')}>Execute authorized rip</button>
                  </div>
                )}
                {['running', 'pause_requested'].includes(savedJob.state) && (
                  <div className="flex gap-3">
                    <button className="btn btn-secondary" disabled={controlling} onClick={() => controlJob('pause')}>Pause after active work</button>
                    <button className="btn btn-secondary" disabled={controlling} onClick={() => controlJob('stop')}>Stop queue</button>
                  </div>
                )}
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
                    <div className="text-[var(--text-muted)]">Runtime cutoff</div>
                    <div className="text-white font-semibold">
                      {drive.minimum_length_seconds === null ? 'Not applicable' : `${drive.minimum_length_seconds}s`}
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>

          <div className="glass-panel rounded-xl overflow-hidden">
            <div className="p-4 border-b border-[var(--border-color)] font-bold text-white">Planned titles</div>
            <div className="divide-y divide-[var(--border-color)]">
              {preview.jobs.map((job) => (
                <div key={job.job_id} className="p-4 grid grid-cols-1 lg:grid-cols-[8rem_1fr_8rem] gap-3 items-center">
                  <div className="font-mono text-sm text-white">Title {job.title_index}</div>
                  <div className="min-w-0">
                    <div className="text-sm text-[var(--text-muted)] truncate">
                      {job.final_destination || job.staging_destination}
                    </div>
                    <div className="text-xs text-[var(--text-muted)] mt-1">{formatBytes(job.estimated_bytes)}</div>
                  </div>
                  <div className={`text-xs font-bold uppercase ${job.collision_status === 'clear'
                    ? 'text-green-400'
                    : job.collision_status === 'not-checked'
                      ? 'text-slate-400'
                      : 'text-amber-400'
                  }`}>
                    {job.collision_status}
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
