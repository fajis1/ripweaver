import { useCallback, useEffect, useState } from 'react';

interface ExpiredCleanupProposal {
  cleanup_due: boolean;
  postponed: boolean;
  postponed_until: string | null;
  ttl_days: number;
  media_ids: string[];
  file_count: number;
  total_size_bytes: number;
  plan_sha256: string | null;
  jellyfin_files_affected: number;
}

const formatBytes = (bytes: number) => `${(bytes / (1024 ** 3)).toFixed(2)} GiB`;

const ExpiredSourceCleanupModal = () => {
  const [proposal, setProposal] = useState<ExpiredCleanupProposal | null>(null);
  const [postponeDays, setPostponeDays] = useState(7);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    try {
      const response = await fetch('/rip/pipeline/retained-sources/expired');
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || 'Expired-original cleanup could not be checked.');
      setProposal(payload.cleanup_due && payload.file_count > 0 ? payload : null);
    } catch (requestError) {
      console.error('Expired-original cleanup check failed:', requestError);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(refresh, 60_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  if (!proposal) return null;

  const postpone = async () => {
    setWorking(true);
    setError('');
    try {
      const response = await fetch('/rip/pipeline/retained-sources/postpone-cleanup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ postpone_days: postponeDays }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || 'Cleanup could not be postponed.');
      setProposal(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Cleanup could not be postponed.');
    } finally {
      setWorking(false);
    }
  };

  const deleteExpired = async () => {
    if (!proposal.plan_sha256) return;
    setWorking(true);
    setError('');
    try {
      const response = await fetch('/rip/pipeline/retained-sources/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          media_ids: proposal.media_ids,
          expected_plan_sha256: proposal.plan_sha256,
          authorized_file_count: proposal.file_count,
          confirm_delete: true,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || 'Expired originals were not deleted.');
      setProposal(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Expired originals were not deleted.');
      await refresh();
    } finally {
      setWorking(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[120] flex items-center justify-center bg-black/80 p-4" role="dialog" aria-modal="true" aria-labelledby="expired-cleanup-title">
      <div className="glass-panel w-full max-w-xl rounded-2xl border border-amber-400/40 p-6 shadow-2xl">
        <h2 id="expired-cleanup-title" className="text-2xl font-bold text-white">Retained originals are ready for cleanup</h2>
        <p className="mt-3 text-[var(--text-muted)]">
          {proposal.file_count} original file(s), totaling {formatBytes(proposal.total_size_bytes)}, have reached the configured {proposal.ttl_days}-day retention period.
        </p>
        <div className="mt-4 rounded-xl border border-amber-400/25 bg-amber-400/10 p-4 text-sm text-amber-100">
          Approving permanently deletes only the retained originals from cleanup staging. Media-library files are not changed. Another encode would require ripping the disc again.
        </div>
        {error && <div className="mt-4 rounded-xl border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">{error}</div>}
        <div className="mt-6 flex flex-wrap items-center justify-end gap-3">
          <select className="input-field" value={postponeDays} onChange={(event) => setPostponeDays(Number(event.target.value))} disabled={working} aria-label="Cleanup postponement">
            <option value={1}>Postpone 1 day</option>
            <option value={7}>Postpone 7 days</option>
            <option value={30}>Postpone 30 days</option>
          </select>
          <button type="button" className="btn btn-secondary" onClick={postpone} disabled={working}>Postpone cleanup</button>
          <button type="button" className="btn btn-primary" onClick={deleteExpired} disabled={working}>Permanently delete expired originals</button>
        </div>
      </div>
    </div>
  );
};

export default ExpiredSourceCleanupModal;
