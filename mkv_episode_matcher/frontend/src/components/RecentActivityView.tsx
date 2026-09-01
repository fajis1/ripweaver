import { useEffect, useMemo, useState } from 'react';

interface HistoryItem {
  media_id: string; display_name: string | null; location_label: string;
  location_relative: string | null; location_root_key: 'jellyfin_tv_root' | 'jellyfin_movie_root' | null;
  state: string; stage: string; updated_at: string; review_code: string | null; error_type: string | null;
  output_size_bytes: number | null;
  retained_source_available: boolean;
  staged_source_available: boolean;
  provisional_match: boolean; gemini_confidence: number | null;
}

interface RecentActivityViewProps { onOpenDashboard?: () => void; }

const hasReviewableMedia = (item: HistoryItem) =>
  item.state !== 'discarded' || item.staged_source_available || item.retained_source_available;

const responsePayload = async (response: Response): Promise<Record<string, unknown>> => {
  const text = await response.text();
  if (!text) return {};
  try { return JSON.parse(text); }
  catch { return { detail: response.ok ? 'The server returned an unreadable response.' : text.slice(0, 200) }; }
};

const RecentActivityView = ({ onOpenDashboard }: RecentActivityViewProps) => {
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [working, setWorking] = useState(false);
  const [geminiProgress, setGeminiProgress] = useState<Record<string, string>>({});
  const [renameDrafts, setRenameDrafts] = useState<Record<string, string>>({});
  const [roots, setRoots] = useState({ tv: '', movie: '' });
  const [retentionDays, setRetentionDays] = useState(30);
  const [readIds, setReadIds] = useState<Set<string>>(() => new Set(JSON.parse(localStorage.getItem('ripweaver-recent-read') || '[]')));
  const [hiddenIds, setHiddenIds] = useState<Set<string>>(() => new Set(JSON.parse(localStorage.getItem('ripweaver-recent-hidden') || '[]')));

  const saveRecentState = (key: string, values: Set<string>) => localStorage.setItem(key, JSON.stringify([...values]));
  const markRead = (mediaIds: string[]) => setReadIds((current) => { const next = new Set([...current, ...mediaIds]); saveRecentState('ripweaver-recent-read', next); return next; });
  const clearRead = () => setHiddenIds((current) => { const next = new Set([...current, ...readIds]); saveRecentState('ripweaver-recent-hidden', next); return next; });
  const clearAll = () => setHiddenIds((current) => { const next = new Set([...current, ...items.map((item) => item.media_id)]); saveRecentState('ripweaver-recent-hidden', next); return next; });
  const restoreCleared = () => { const next = new Set<string>(); saveRecentState('ripweaver-recent-hidden', next); setHiddenIds(next); };

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const schedule = () => {
      if (!cancelled && document.visibilityState !== 'hidden') timer = window.setTimeout(refresh, 12_000);
    };
    const refresh = async () => {
      if (cancelled || document.visibilityState === 'hidden') return;
      try {
        const [response, configResponse] = await Promise.all([fetch('/rip/pipeline/items'), fetch('/system/config')]);
        const payload = await response.json();
        if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'History could not be loaded.');
        if (cancelled) return;
        setItems((payload.items as HistoryItem[]).filter(hasReviewableMedia));
        setNotice((current) => {
          if (!current.startsWith('Queued evidence and Gemini review')) return current;
          if (payload.items.some((item: HistoryItem) => item.review_code === 'gemini_analysis_running')) return 'Local evidence collection and Gemini review are running.';
          if (payload.items.some((item: HistoryItem) => ['gemini_analysis_interrupted', 'gemini_analysis_failed', 'gemini_audio_evidence_insufficient', 'gemini_catalog_unavailable', 'gemini_provider_failed', 'gemini_credential_rejected', 'gemini_rate_limited', 'gemini_provider_unavailable', 'gemini_request_rejected', 'gemini_network_failed', 'gemini_response_invalid', 'whole_disc_coherence_review_required'].includes(item.review_code || ''))) return 'Episode matching stopped safely for review. Open the affected disc or Logs for the specific reason.';
          if (!payload.items.some((item: HistoryItem) => ['gemini_evidence_required', 'gemini_analysis_running'].includes(item.review_code || ''))) return 'Gemini review finished and successful matches returned to the pipeline.';
          return current;
        });
        if (configResponse.ok) {
          const config = await configResponse.json();
          setRoots({ tv: config.jellyfin_tv_root || '', movie: config.jellyfin_movie_root || '' });
          setRetentionDays(Number(config.retained_source_ttl_days || 30));
        }
        setError('');
      } catch (requestError) { if (!cancelled) setError(requestError instanceof Error ? requestError.message : 'History could not be loaded.'); }
      finally { schedule(); }
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
  }, []);

  const fullPath = (item: HistoryItem) => {
    if (!item.location_relative || !item.location_root_key) return null;
    const root = item.location_root_key === 'jellyfin_tv_root' ? roots.tv : roots.movie;
    return root ? `${root.replace(/[\\/]+$/, '')}\\${item.location_relative.replaceAll('/', '\\')}` : null;
  };
  const discKeyFor = (mediaId: string) => mediaId.includes('-title-') ? mediaId.slice(0, mediaId.lastIndexOf('-title-')) : mediaId;
  const discName = (discItems: HistoryItem[]) => discItems.find((item) => item.location_relative)?.location_relative?.split('/')[0] || `Disc ${discKeyFor(discItems[0]?.media_id || 'result')}`;
  const formatBytes = (bytes: number | null) => bytes === null ? null : `${(bytes / (1024 ** 3)).toFixed(2)} GiB`;

  const groups = useMemo(() => {
    const grouped = new Map<string, Map<string, HistoryItem[]>>();
    [...items].filter((item) => !hiddenIds.has(item.media_id)).sort((a, b) => b.updated_at.localeCompare(a.updated_at)).forEach((item) => {
      const day = new Date(item.updated_at).toLocaleDateString(undefined, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
      const discKey = discKeyFor(item.media_id);
      const discs = grouped.get(day) || new Map<string, HistoryItem[]>();
      discs.set(discKey, [...(discs.get(discKey) || []), item]); grouped.set(day, discs);
    });
    return [...grouped.entries()];
  }, [items, hiddenIds]);

  const runGeminiReview = async (mediaId: string) => {
    if (!window.confirm('Read only the held verified MKV(s), collect bounded local audio evidence, and send only short excerpts plus allowed catalogue names to Gemini? No full MKV or local path is transmitted.')) return;
    setWorking(true); setError(''); setGeminiProgress((current) => ({ ...current, [mediaId]: 'Submitting retry…' }));
    try {
      const response = await fetch('/rip/pipeline/gemini/execute', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ media_ids: [mediaId], confirm_media_read: true, confirm_external_transmission: true }) });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Gemini review could not be queued.');
      setNotice(`Queued evidence and Gemini review for ${Number(payload.item_count || 0)} held title(s).`);
      setGeminiProgress((current) => ({ ...current, [mediaId]: 'Collecting local evidence and preparing the Gemini request…' }));
    } catch (requestError) { const message = requestError instanceof Error ? requestError.message : 'Gemini review could not be queued.'; setError(message); setGeminiProgress((current) => ({ ...current, [mediaId]: `Retry did not start: ${message}` })); }
    finally { setWorking(false); }
  };
  const playReview = async (mediaId: string) => { if (!window.confirm('Open this exact recorded MKV in the Windows default media player? No file will be changed.')) return; const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(mediaId)}/play-review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm_play: true }) }); const payload = await response.json(); if (!response.ok) setError(payload.detail || 'Review playback could not start.'); };
  const renameProvisional = async (mediaId: string) => { const newName = (renameDrafts[mediaId] || '').trim(); if (!newName || !window.confirm(`Rename the Jellyfin file to “${newName}.mkv”? Existing files will not be overwritten.`)) return; const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(mediaId)}/rename-provisional`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ new_name: newName, confirm_rename: true }) }); const payload = await response.json(); if (!response.ok) setError(payload.detail || 'Rename failed safely.'); else { setNotice('Reviewed Jellyfin name updated.'); setRenameDrafts((current) => ({ ...current, [mediaId]: '' })); } };
  const deletePreservedStagedRip = async (item: HistoryItem) => {
    const title = item.display_name || item.media_id;
    if (!window.confirm(`Permanently delete the preserved staged rip for “${title}”? Jellyfin is not changed. This cannot be undone, and reripping will be required to recover it.`)) return;
    setWorking(true); setError('');
    try {
      const response = await fetch(`/rip/pipeline/items/${encodeURIComponent(item.media_id)}/delete-staged-source`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm_delete: true }) });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'The preserved staged rip could not be deleted safely.');
      setItems(payload.items);
      setNotice(`Deleted the preserved staged rip for “${title}”. Jellyfin was not changed.`);
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'The preserved staged rip could not be deleted safely.'); }
    finally { setWorking(false); }
  };

  const reencodeDisc = async (discItems: HistoryItem[]) => {
    const mediaIds = discItems.filter((item) => item.retained_source_available).map((item) => item.media_id);
    if (!mediaIds.length || !window.confirm(`Queue ${mediaIds.length} retained original(s) for a fresh HandBrake encode? Saved matched names will be reused. Jellyfin files will not be replaced or deleted. Retained originals expire after ${retentionDays} day(s).`)) return;
    setWorking(true); setError('');
    try {
      const response = await fetch('/rip/pipeline/retained-sources/reencode', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ media_ids: mediaIds, confirm_reencode: true }) });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Re-encode could not be queued.');
      setNotice(`Queued ${payload.queued_item_count} retained original(s) for profile review and re-encoding.`);
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Re-encode could not be queued.'); }
    finally { setWorking(false); }
  };

  const deleteDiscSources = async (discItems: HistoryItem[]) => {
    const mediaIds = discItems.filter((item) => item.retained_source_available).map((item) => item.media_id);
    if (!mediaIds.length) return;
    setWorking(true); setError('');
    try {
      const previewResponse = await fetch('/rip/pipeline/retained-sources/preview-delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ media_ids: mediaIds }) });
      const preview = await previewResponse.json();
      if (!previewResponse.ok) throw new Error(typeof preview.detail === 'string' ? preview.detail : 'Deletion review could not be prepared.');
      const size = formatBytes(preview.total_size_bytes);
      if (!window.confirm(`Permanently delete exactly ${preview.file_count} retained original(s) (${size}) for this disc? Jellyfin files are not affected. These originals will no longer be available for re-encoding without ripping the disc again.`)) return;
      const response = await fetch('/rip/pipeline/retained-sources/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ media_ids: mediaIds, expected_plan_sha256: preview.plan_sha256, authorized_file_count: preview.file_count, confirm_delete: true }) });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Retained originals were not deleted.');
      setNotice(`Deleted ${payload.deleted_file_count} retained original(s). Jellyfin files were not changed.`);
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Retained originals were not deleted.'); }
    finally { setWorking(false); }
  };

  return <div className="space-y-6 animate-fade-in">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-3xl font-bold heading-gradient">Recently Finished</h2><p className="mt-1 text-sm text-[var(--text-muted)]">Results are grouped by day and disc and remain available after ejection. Read and cleared state is saved in this browser; clearing does not delete media or pipeline history.</p></div><div className="flex flex-wrap gap-2"><button type="button" className="btn btn-secondary text-xs" onClick={() => markRead(items.map((item) => item.media_id))}>Mark all read</button><button type="button" className="btn btn-secondary text-xs" disabled={readIds.size === 0} onClick={clearRead}>Clear read from view</button><button type="button" className="btn btn-secondary text-xs" disabled={items.length === 0} onClick={clearAll}>Clear all from view</button>{hiddenIds.size > 0 && <button type="button" className="btn btn-secondary text-xs" onClick={restoreCleared}>Show cleared history</button>}</div></div>
    {items.some((item) => item.stage === 'transcode' && item.state === 'queued') && <div className="rounded-xl border border-blue-400/30 bg-blue-400/10 p-4 text-blue-100"><div className="font-semibold">Provisional matches are ready for transcoding.</div><div className="mt-1 text-sm">Choose the exact HandBrake profile and authorize the queued batch from the Disc Dashboard.</div>{onOpenDashboard && <button type="button" className="btn btn-primary mt-3" onClick={onOpenDashboard}>Review profile and start queued transcodes</button>}</div>}
    {error && <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-red-200">{error}</div>}
    {notice && <div className="rounded-xl border border-blue-500/30 bg-blue-500/10 p-4 text-blue-100">{notice}</div>}
    {groups.length === 0 && <div className="glass-panel rounded-xl p-6 text-[var(--text-muted)]">No pipeline results have been recorded yet.</div>}
    {groups.map(([day, discs]) => <section key={day} className="space-y-3"><div className="text-lg font-bold text-white">{day}</div>
      {[...discs.entries()].map(([discKey, discItems]) => {
        const completed = discItems.filter((item) => item.state === 'completed').length; const attention = discItems.length - completed;
        const retained = discItems.filter((item) => item.retained_source_available).length;
        const unread = discItems.filter((item) => !readIds.has(item.media_id)).length;
        return <details key={discKey} className="glass-panel rounded-xl overflow-hidden" open={attention > 0} onToggle={(event) => { if (event.currentTarget.open) markRead(discItems.map((item) => item.media_id)); }}>
          <summary className="cursor-pointer list-none border-b border-[var(--border-color)] p-4"><div className="flex flex-wrap items-center justify-between gap-3"><div><div className="font-bold text-white">{discName(discItems)}{unread > 0 && <span className="ml-2 rounded-full bg-blue-500/20 px-2 py-0.5 text-xs text-blue-200">{unread} unread</span>}</div><div className="mt-1 font-mono text-[11px] text-[var(--text-muted)]">Disc record: {discKey}</div></div><div className="text-sm"><span className="text-green-300">{completed} completed</span>{attention > 0 && <span className="ml-3 text-amber-300">{attention} need attention</span>}{retained > 0 && <span className="ml-3 text-blue-200">{retained} retained originals</span>}</div></div></summary>
          {retained > 0 && <div className="flex flex-wrap gap-2 border-b border-[var(--border-color)] p-4"><button type="button" className="btn btn-primary text-xs" disabled={working} onClick={() => reencodeDisc(discItems)}>Re-encode this disc</button><button type="button" className="btn btn-secondary text-xs" disabled={working} onClick={() => deleteDiscSources(discItems)}>Delete retained originals</button><div className="w-full text-xs text-amber-200">Re-encode keeps Jellyfin unchanged and queues a new HandBrake review. Delete is permanent for retained originals only and requires exact confirmation. Retained originals expire after {retentionDays} day(s).</div></div>}
          <div className="divide-y divide-[var(--border-color)]">{discItems.map((item) => <div key={item.media_id} className="grid gap-3 p-4 md:grid-cols-[1fr_auto]">
            <div><div className="font-semibold text-white">{item.display_name || 'Unmatched title'}</div><div className="mt-1 font-mono text-[11px] text-[var(--text-muted)]">{item.media_id}</div><div className="mt-2 text-sm text-blue-100">{item.location_label}{item.location_relative ? ` / ${item.location_relative}` : ''}</div>{fullPath(item) && <div className="mt-2 break-all font-mono text-[11px] text-blue-50">{fullPath(item)}</div>}{formatBytes(item.output_size_bytes) && <div className="mt-2 text-sm text-green-200">Finished file size: {formatBytes(item.output_size_bytes)}</div>}{(item.review_code || item.error_type) && <div className="mt-2 text-xs text-amber-300">Why it stopped: {item.review_code || item.error_type}</div>}
              {['gemini_evidence_required', 'gemini_analysis_interrupted', 'gemini_analysis_failed', 'gemini_audio_evidence_insufficient', 'gemini_catalog_unavailable', 'gemini_provider_failed', 'gemini_credential_rejected', 'gemini_rate_limited', 'gemini_provider_unavailable', 'gemini_request_rejected', 'gemini_network_failed', 'gemini_response_invalid'].includes(item.review_code || '') && <button type="button" className="btn btn-primary mt-3 text-xs" disabled={working} onClick={() => runGeminiReview(item.media_id)}>{item.review_code === 'gemini_evidence_required' ? 'Run local evidence and Gemini review' : 'Retry local evidence and Gemini review'}</button>}
              {geminiProgress[item.media_id] && <div className="mt-2 rounded border border-blue-400/30 bg-blue-400/10 p-2 text-xs text-blue-100">{geminiProgress[item.media_id]}</div>}
              {item.review_code === 'gemini_analysis_running' && <div className="mt-3 text-xs text-blue-200">Local evidence and Gemini review are running.</div>}
              {item.provisional_match && <div className="mt-3 rounded border border-amber-400/30 bg-amber-400/10 p-3 text-sm text-amber-100"><div>Gemini provisional match{item.gemini_confidence !== null ? ` · ${Math.round(item.gemini_confidence * 100)}% confidence` : ''}.</div><div className="mt-2 flex flex-wrap gap-2"><button type="button" className="btn btn-secondary text-xs" onClick={() => playReview(item.media_id)}>Play for review</button><input className="input-field min-w-64 text-xs" value={renameDrafts[item.media_id] || ''} onChange={(event) => setRenameDrafts((current) => ({ ...current, [item.media_id]: event.target.value }))} placeholder="New filename (without .mkv)" /><button type="button" className="btn btn-primary text-xs" disabled={!renameDrafts[item.media_id]?.trim()} onClick={() => renameProvisional(item.media_id)}>Rename reviewed file</button></div></div>}
              {item.stage === 'transcode' && item.state === 'queued' && onOpenDashboard && <button type="button" className="btn btn-primary mt-3 text-xs" onClick={onOpenDashboard}>Choose profile and start transcode</button>}
              {item.state === 'discarded' && item.staged_source_available && <button type="button" className="btn mt-3 text-xs border border-red-400/50 bg-red-500/15 text-red-100 hover:bg-red-500/25" disabled={working} onClick={() => deletePreservedStagedRip(item)}>Delete preserved staged rip</button>}
            </div><div className="text-right text-xs text-[var(--text-muted)]"><div className={item.state === 'completed' ? 'text-green-300 font-semibold' : item.state === 'review_required' ? 'text-amber-300 font-semibold' : 'text-blue-200 font-semibold'}>{item.stage} · {item.state.replaceAll('_', ' ')}</div><div className="mt-1">{new Date(item.updated_at).toLocaleString()}</div></div>
          </div>)}</div>
        </details>;
      })}
    </section>)}
  </div>;
};

export default RecentActivityView;
