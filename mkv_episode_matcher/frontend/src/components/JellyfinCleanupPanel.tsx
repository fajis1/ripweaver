import { useMemo, useState } from 'react';

interface Candidate {
  category: string;
  relative_path: string;
  library_relative: string;
  size_bytes: number;
  modified_at: string;
  backed_up: boolean;
  disk_key: string;
  disk_label: string;
  candidate_key: string;
}

interface CleanupPreview {
  plan_sha256: string;
  file_count: number;
  total_size_bytes: number;
  candidates: Candidate[];
}

interface CandidateGroup {
  key: string;
  title: string;
  candidates: Candidate[];
}

interface DiskGroup extends CandidateGroup {
  statusGroups: CandidateGroup[];
}

const formatBytes = (bytes: number) => `${(bytes / (1024 ** 3)).toFixed(2)} GiB`;

const JellyfinCleanupPanel = () => {
  const [mode, setMode] = useState<'older_than' | 'all' | 'all_staging'>('older_than');
  const [days, setDays] = useState<7 | 14>(7);
  const [preview, setPreview] = useState<CleanupPreview | null>(null);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [working, setWorking] = useState(false);

  const groups = useMemo<DiskGroup[]>(() => {
    if (!preview) return [];
    const grouped = new Map<string, Candidate[]>();
    preview.candidates.forEach((candidate) => {
      const key = candidate.disk_key;
      grouped.set(key, [...(grouped.get(key) || []), candidate]);
    });
    return [...grouped.entries()].map(([key, candidates]) => ({
      key,
      title: candidates[0].disk_label,
      candidates,
      statusGroups: [...new Map(
        candidates.reduce((entries, candidate) => {
          const status = candidate.backed_up ? 'Backed up' : 'Not backed up';
          const existing = entries.get(status) || [];
          entries.set(status, [...existing, candidate]);
          return entries;
        }, new Map<string, Candidate[]>())
      ).entries()].map(([status, statusCandidates]) => ({ key: `${key}:${status}`, title: status, candidates: statusCandidates })),
    }));
  }, [preview]);

  const requestPreview = async (candidateKeys?: string[]) => {
    const response = await fetch('/system/cleanup/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode, days: mode === 'older_than' ? days : null, candidate_keys: candidateKeys }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Cleanup scan failed.');
    return payload as CleanupPreview;
  };

  const scan = async () => {
    setWorking(true); setError(''); setMessage(''); setPreview(null);
    try { setPreview(await requestPreview()); }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Cleanup scan failed.'); }
    finally { setWorking(false); }
  };

  const executeDelete = async (plan: CleanupPreview, candidateKeys?: string[]) => {
    const scope = mode === 'all_staging' ? 'staging files' : mode === 'all' ? 'backed-up files' : `files older than ${days} days`;
    const warning = mode === 'all_staging' ? 'This includes files without media-library backups and may require reripping.' : 'Only staging files with verified media-library counterparts will be removed.';
    if (!window.confirm(`Permanently delete ${plan.file_count} ${scope}, totaling ${formatBytes(plan.total_size_bytes)}? ${warning}`)) return;
    setWorking(true); setError(''); setMessage('');
    try {
      const response = await fetch('/system/cleanup/delete', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode, days: mode === 'older_than' ? days : null, candidate_keys: candidateKeys, expected_plan_sha256: plan.plan_sha256, authorized_file_count: plan.file_count, confirm_delete: true }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || 'Cleanup was stopped safely.');
      setMessage(`Deleted ${payload.deleted_file_count} staging file(s). Media-library files were not changed.`);
      setPreview(null);
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Cleanup was stopped safely.'); }
    finally { setWorking(false); }
  };

  const removeAll = () => { if (preview && preview.file_count > 0) void executeDelete(preview); };

  const removeGroup = async (group: CandidateGroup) => {
    setWorking(true); setError('');
    try {
      const groupPreview = await requestPreview(group.candidates.map((candidate) => candidate.candidate_key));
      setPreview(groupPreview);
      await executeDelete(groupPreview, group.candidates.map((candidate) => candidate.candidate_key));
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Group cleanup scan failed.'); }
    finally { setWorking(false); }
  };

  return <section className="glass-panel rounded-xl p-5 space-y-4">
    <div><h3 className="text-xl font-bold text-white">Media-library-backed staging cleanup</h3><p className="mt-1 text-sm text-[var(--text-muted)]">Candidates are grouped by backup status and disc. The unbacked option includes every MKV in configured staging roots, excluding known test and sample folders. Scanning is read-only; deletion requires a fresh plan and confirmation.</p></div>
    {error && <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-red-200">{error}</div>}
    {message && <div className="rounded-lg border border-green-500/30 bg-green-500/10 p-3 text-green-200">{message}</div>}
    <div className="flex flex-wrap items-center gap-3">
      <select className="input-field" value={mode} onChange={(event) => setMode(event.target.value as 'older_than' | 'all' | 'all_staging')}><option value="older_than">Older than</option><option value="all">All backed-up files</option><option value="all_staging">All staging files, including unbacked</option></select>
      {mode === 'older_than' && <select className="input-field" value={days} onChange={(event) => setDays(Number(event.target.value) as 7 | 14)}><option value={7}>7 days</option><option value={14}>14 days</option></select>}
      <button type="button" className="btn btn-secondary" disabled={working} onClick={scan}>Scan staging roots</button>
    </div>
    {preview && <div className="space-y-3 rounded-lg border border-[var(--border-color)] p-4"><div className="flex flex-wrap items-center justify-between gap-3"><div className="text-sm text-white">{preview.file_count} candidate(s) · {formatBytes(preview.total_size_bytes)}</div><button type="button" className="btn btn-primary" disabled={working || preview.file_count === 0} onClick={removeAll}>Delete all reviewed candidates</button></div>{preview.file_count === 0 ? <div className="text-sm text-[var(--text-muted)]">No eligible staging files were found.</div> : <div className="space-y-2">{groups.map((group) => <details key={group.key} className="rounded-lg border border-[var(--border-color)]"><summary className="flex cursor-pointer items-center justify-between gap-3 p-3 text-sm"><span className="font-semibold text-white">{group.title}</span><span className="text-[var(--text-muted)]">{group.candidates.length} file(s) · {formatBytes(group.candidates.reduce((total, candidate) => total + candidate.size_bytes, 0))}</span></summary><div className="border-t border-[var(--border-color)] p-3 space-y-3"><div className="flex justify-end"><button type="button" className="btn btn-secondary" disabled={working} onClick={() => void removeGroup(group)}>Delete this disk</button></div>{group.statusGroups.map((statusGroup) => <details key={statusGroup.key} className="rounded-lg border border-[var(--border-color)]"><summary className="flex cursor-pointer items-center justify-between gap-3 p-3 text-sm"><span className="font-semibold text-white">{statusGroup.title}</span><span className="text-[var(--text-muted)]">{statusGroup.candidates.length} file(s) · {formatBytes(statusGroup.candidates.reduce((total, candidate) => total + candidate.size_bytes, 0))}</span></summary><div className="border-t border-[var(--border-color)] p-3"><div className="mb-3 flex justify-end"><button type="button" className="btn btn-secondary" disabled={working} onClick={() => void removeGroup(statusGroup)}>Delete this backup group</button></div><div className="max-h-64 divide-y divide-[var(--border-color)] overflow-auto">{statusGroup.candidates.map((candidate) => <div key={candidate.candidate_key} className="py-2 text-sm"><div className="text-white">{candidate.category}: {candidate.relative_path}</div><div className="text-[var(--text-muted)]">{candidate.library_relative ? `Media library: ${candidate.library_relative} · ` : 'No verified media-library counterpart · '}{formatBytes(candidate.size_bytes)}</div></div>)}</div></div></details>)}</div></details>)}</div>}</div>}
  </section>;
};

export default JellyfinCleanupPanel;
