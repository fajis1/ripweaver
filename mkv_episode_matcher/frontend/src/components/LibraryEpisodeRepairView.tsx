import { useEffect, useMemo, useState } from 'react';

type AuditStatus = 'pending' | 'confirmed' | 'mismatch' | 'inconclusive';

interface AuditCandidate {
  file_id: string;
  relative_path: string;
  series_name: string;
  season: number;
  episode: number;
  episode_id: string;
  size_bytes: number;
  generic_name: string;
  status: AuditStatus;
  score: number | null;
  qualifying_window_count: number;
  evidence_window_count: number;
  reason: string;
  renamed: boolean;
}

interface AuditJob {
  job_id: string;
  status: 'discovered' | 'running' | 'completed' | 'failed' | 'applied';
  scope: 'sequence-derived' | 'all-named';
  candidate_digest: string;
  result_digest: string | null;
  candidate_count: number;
  progress: { current: number; total: number; relative_path: string | null };
  candidates: AuditCandidate[];
  error_code: string | null;
}

interface Props {
  onBackToStandard: () => void;
}

const detailFor = (candidate: AuditCandidate) => {
  if (candidate.status === 'confirmed') return 'Two independent subtitle windows support this filename, or one window was definitive.';
  if (candidate.status === 'mismatch') return 'Multiple usable windows failed to support the episode claimed by this filename.';
  if (candidate.status === 'inconclusive') return 'The claimed name was not confirmed, but the evidence is not strong enough to call it wrong.';
  return 'Waiting for verification.';
};

export default function LibraryEpisodeRepairView({ onBackToStandard }: Props) {
  const [job, setJob] = useState<AuditJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const request = async (url: string, init?: RequestInit): Promise<AuditJob> => {
    const response = await fetch(url, init);
    const payload = await response.json();
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Episode repair request failed.');
    return payload;
  };

  useEffect(() => {
    if (!job || job.status !== 'running') return;
    const timer = window.setInterval(async () => {
      try {
        const next = await request(`/scan/episode-audit/${job.job_id}`);
        setJob(next);
        if (next.status === 'completed') {
          setSelected(new Set(next.candidates.filter(item => item.status === 'mismatch').map(item => item.file_id)));
        }
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : 'Could not refresh the episode audit.');
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [job]);

  const selectedCandidates = useMemo(
    () => job?.candidates.filter(candidate => selected.has(candidate.file_id)) ?? [],
    [job, selected],
  );

  const discover = async () => {
    setBusy(true);
    setError(null);
    try {
      setJob(await request('/scan/episode-audit/discover', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scope: 'sequence-derived' }),
      }));
      setSelected(new Set());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not inventory the Jellyfin TV folders.');
    } finally {
      setBusy(false);
    }
  };

  const start = async () => {
    if (!job || job.candidate_count === 0) return;
    if (!window.confirm(`Read audio from the exact ${job.candidate_count} MKV files listed below and compare each current SxxExx claim with OpenSubtitles? This check will not rename, move, delete, or transcode anything.`)) return;
    setBusy(true);
    setError(null);
    try {
      setJob(await request(`/scan/episode-audit/${job.job_id}/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          candidate_digest: job.candidate_digest,
          confirm_media_read: true,
          confirm_provider_lookup: true,
        }),
      }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not start the episode audit.');
    } finally {
      setBusy(false);
    }
  };

  const toggle = (candidate: AuditCandidate) => {
    if (!['mismatch', 'inconclusive'].includes(candidate.status) || candidate.renamed) return;
    setSelected(previous => {
      const next = new Set(previous);
      if (next.has(candidate.file_id)) next.delete(candidate.file_id);
      else next.add(candidate.file_id);
      return next;
    });
  };

  const apply = async () => {
    if (!job?.result_digest || selectedCandidates.length === 0) return;
    const names = selectedCandidates.map(item => `${item.relative_path}\n  → ${item.generic_name}`).join('\n\n');
    if (!window.confirm(`Rename exactly these ${selectedCandidates.length} checked files in place? No destination may already exist.\n\n${names}\n\nThe generic files can then be revisited with the standard Season 0X scan.`)) return;
    setBusy(true);
    setError(null);
    try {
      setJob(await request(`/scan/episode-audit/${job.job_id}/apply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          result_digest: job.result_digest,
          file_ids: selectedCandidates.map(item => item.file_id),
          confirm_generic_rename: true,
        }),
      }));
      setSelected(new Set());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not apply the generic names.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="h-full flex flex-col gap-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold heading-gradient mb-1">Verify Existing Episode Names</h2>
          <p className="text-sm text-[var(--text-muted)] max-w-3xl">
            This repair channel finds Jellyfin episodes whose RipWeaver history contains a sequence-derived match from the affected discs. It tests only the episode already claimed by the filename; disc order and sequence matching are never identification evidence here.
          </p>
        </div>
        <div className="flex flex-wrap gap-3 justify-end">
          <button className="btn btn-secondary" onClick={onBackToStandard}>Standard Season 0X scan</button>
          <button className="btn btn-primary" onClick={discover} disabled={busy || job?.status === 'running'}>
            {job ? 'Build fresh inventory' : 'Inventory Jellyfin TV folders'}
          </button>
        </div>
      </div>

      {error && <div className="p-4 rounded-xl border border-red-500/30 bg-red-500/10 text-red-200">{error}</div>}

      {!job && (
        <div className="glass-panel rounded-2xl p-8 max-w-3xl">
          <h3 className="text-xl font-bold text-white">A separate fix path</h3>
          <p className="mt-3 text-[var(--text-muted)]">The inventory step reads filenames, private match provenance, and file metadata only. It searches every folder under the configured Jellyfin TV root for the affected episode IDs. You will see the exact list before approving any Whisper audio reads or subtitle lookups.</p>
          <p className="mt-3 text-[var(--text-muted)]">Confirmed names remain untouched. Clear mismatches are selected for a collision-refusing generic rename. Inconclusive files remain unselected unless you explicitly choose them.</p>
        </div>
      )}

      {job && (
        <div className="flex-1 min-h-0 flex flex-col rounded-xl border border-[var(--border-color)] bg-[var(--bg-secondary)]/40 overflow-hidden">
          <div className="p-4 border-b border-[var(--border-color)] bg-[var(--bg-tertiary)]/40 flex flex-wrap items-center justify-between gap-4">
            <div>
              <div className="font-bold text-white">{job.candidate_count} episode-named MKV{job.candidate_count === 1 ? '' : 's'}</div>
              <div className="text-xs text-[var(--text-muted)] mt-1">
                {job.status === 'discovered' && 'Filename-only inventory ready for exact-file approval.'}
                {job.status === 'running' && `Verifying ${job.progress.current} of ${job.progress.total}${job.progress.relative_path ? ` · ${job.progress.relative_path}` : ''}`}
                {job.status === 'completed' && 'Verification complete. Review every proposed generic name before applying.'}
                {job.status === 'applied' && 'The checked generic renames were applied. Use the standard Season 0X scan when ready.'}
                {job.status === 'failed' && `Audit stopped safely (${job.error_code || 'unknown failure'}). No names were changed.`}
              </div>
            </div>
            <div className="flex gap-3">
              {job.status === 'discovered' && <button className="btn btn-primary" onClick={start} disabled={busy || job.candidate_count === 0}>Verify exact listed files</button>}
              {job.status === 'completed' && <button className="btn btn-primary" onClick={apply} disabled={busy || selectedCandidates.length === 0}>Apply {selectedCandidates.length} checked generic rename{selectedCandidates.length === 1 ? '' : 's'}</button>}
            </div>
          </div>

          {job.status === 'running' && (
            <div className="h-2 bg-gray-800">
              <div className="h-full bg-gradient-to-r from-blue-500 to-purple-500 transition-all" style={{ width: `${job.progress.total ? (job.progress.current / job.progress.total) * 100 : 0}%` }} />
            </div>
          )}

          <div className="flex-1 overflow-auto p-4 space-y-3">
            {job.candidates.length === 0 && <div className="text-[var(--text-muted)] p-6">No current Jellyfin MKVs correspond to the sequence-derived episode claims retained in RipWeaver history.</div>}
            {job.candidates.map(candidate => {
              const eligible = ['mismatch', 'inconclusive'].includes(candidate.status) && !candidate.renamed;
              const checked = selected.has(candidate.file_id);
              return (
                <div key={candidate.file_id} className={`p-4 rounded-xl border ${candidate.status === 'confirmed' ? 'border-green-500/25 bg-green-500/5' : candidate.status === 'mismatch' ? 'border-red-500/30 bg-red-500/5' : candidate.status === 'inconclusive' ? 'border-yellow-500/30 bg-yellow-500/5' : 'border-[var(--border-color)] bg-[var(--bg-tertiary)]/20'}`}>
                  <div className="flex items-start gap-3">
                    {(job.status === 'completed' || job.status === 'applied') && (
                      <input type="checkbox" className="mt-1" checked={checked} disabled={!eligible} onChange={() => toggle(candidate)} aria-label={`Use a generic name for ${candidate.relative_path}`} />
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="font-medium text-white break-all">{candidate.relative_path}</div>
                      <div className="text-sm text-[var(--text-muted)] mt-1">Claimed episode: {candidate.series_name} · {candidate.episode_id}</div>
                      {candidate.status !== 'pending' && (
                        <div className="text-sm mt-2">
                          <span className={candidate.status === 'confirmed' ? 'text-green-300' : candidate.status === 'mismatch' ? 'text-red-300' : 'text-yellow-300'}>{candidate.status === 'mismatch' ? 'Mismatch' : candidate.status.charAt(0).toUpperCase() + candidate.status.slice(1)}</span>
                          <span className="text-[var(--text-muted)]"> · {detailFor(candidate)}</span>
                          {candidate.score !== null && <span className="text-[var(--text-muted)]"> Best window {(candidate.score * 100).toFixed(0)}%; {candidate.qualifying_window_count} of {candidate.evidence_window_count} windows qualified.</span>}
                        </div>
                      )}
                      {eligible && <div className="text-xs text-blue-200 mt-2">Generic preview: {candidate.generic_name}</div>}
                      {candidate.renamed && <div className="text-xs text-green-300 mt-2">Renamed to {candidate.generic_name}</div>}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
