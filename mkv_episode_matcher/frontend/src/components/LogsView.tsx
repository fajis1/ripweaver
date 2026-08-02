import { useEffect, useMemo, useState } from 'react';

interface PipelineEvent {
  sequence: number; media_id: string; event_type: string; stage: string;
  state: string; created_at: string; details: Record<string, unknown>;
}

const reasonText: Record<string, string> = {
  gemini_analysis_failed: 'This older run recorded only a general pipeline failure. The exact reason was not retained.',
  gemini_audio_evidence_insufficient: 'Local transcription did not produce enough usable dialogue to make a safe Gemini request.',
  gemini_catalog_unavailable: 'The reviewed bonus-feature catalogue was missing or no longer matched this disc.',
  gemini_descriptive_review_required: 'Gemini reviewed the bounded evidence but could not safely classify the title as a movie or bonus feature.',
  gemini_provider_failed: 'The external Gemini request failed safely. Check the configured key, network access, provider status, and retry later.',
  special_feature_evidence_required: 'More local evidence is required before a bonus-feature name can be assigned.',
};

const LogsView = () => {
  const [events, setEvents] = useState<PipelineEvent[]>([]);
  const [error, setError] = useState('');
  const [filter, setFilter] = useState('');

  useEffect(() => {
    const refresh = async () => {
      try {
        const response = await fetch('/rip/pipeline/events'); const payload = await response.json();
        if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Logs could not be loaded.');
        setEvents(payload.events || []); setError('');
      } catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Logs could not be loaded.'); }
    };
    void refresh(); const timer = window.setInterval(refresh, 3000); return () => window.clearInterval(timer);
  }, []);

  const visible = useMemo(() => {
    const query = filter.trim().toLowerCase();
    return [...events].reverse().filter((event) => !query || `${event.media_id} ${event.event_type} ${event.stage} ${event.state} ${JSON.stringify(event.details)}`.toLowerCase().includes(query));
  }, [events, filter]);

  return <div className="space-y-5 animate-fade-in">
    <div><h2 className="text-3xl font-bold heading-gradient">Logs</h2><p className="mt-1 text-sm text-[var(--text-muted)]">Durable, path-redacted pipeline events. Credentials, dialogue, and private media paths are not shown.</p></div>
    {error && <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-red-200">{error}</div>}
    <input className="input-field w-full" value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="Filter by recovery ID, stage, state, or reason" />
    <div className="glass-panel rounded-xl overflow-hidden">{visible.length === 0 ? <div className="p-5 text-[var(--text-muted)]">No matching events.</div> : <div className="divide-y divide-[var(--border-color)]">{visible.map((event) => {
      const reviewCode = typeof event.details.review_code === 'string' ? event.details.review_code : null;
      const errorType = typeof event.details.error_type === 'string' ? event.details.error_type : null;
      const reason = reviewCode || errorType;
      return <div key={event.sequence} className="grid gap-3 p-4 md:grid-cols-[10rem_1fr_auto]"><div className="text-xs text-[var(--text-muted)]">{new Date(event.created_at).toLocaleString()}</div><div><div className="font-mono text-sm text-white">{event.media_id}</div><div className="mt-1 text-sm text-blue-100">{event.event_type.replaceAll('_', ' ')} · {event.stage} · {event.state.replaceAll('_', ' ')}</div>{reason && <div className="mt-2 text-sm text-amber-200"><span className="font-mono">{reason}</span>: {reasonText[reason] || 'The item stopped safely and requires review.'}</div>}</div><div className="font-mono text-xs text-[var(--text-muted)]">#{event.sequence}</div></div>;
    })}</div>}</div>
  </div>;
};

export default LogsView;
