import { useCallback, useEffect, useState } from 'react';

interface HealthItem {
  id: string;
  field: string;
  category: 'tool' | 'storage' | 'provider';
  label: string;
  feature: string;
  status: 'ready' | 'available' | 'missing' | 'invalid' | 'optional';
  required: boolean;
  message: string;
  download_url: string | null;
}

interface SystemHealth {
  status: 'ready' | 'needs_setup';
  summary: string;
  ready_for: Record<string, boolean>;
  items: HealthItem[];
}

interface SetupHealthViewProps {
  onOpenSettings: () => void;
}

const categoryTitles: Record<HealthItem['category'], string> = {
  tool: 'External tools',
  storage: 'Folders and media libraries',
  provider: 'Identification providers',
};

const statusStyles: Record<HealthItem['status'], string> = {
  ready: 'border-green-500/30 bg-green-500/10 text-green-200',
  available: 'border-blue-500/30 bg-blue-500/10 text-blue-200',
  missing: 'border-amber-500/30 bg-amber-500/10 text-amber-100',
  invalid: 'border-red-500/30 bg-red-500/10 text-red-100',
  optional: 'border-[var(--border-color)] bg-[var(--bg-tertiary)]/30 text-[var(--text-muted)]',
};

const featureLabels: Record<string, string> = {
  disc_ripping: 'Disc ripping',
  transcoding: 'Transcoding',
  media_analysis: 'Media analysis',
  media_organization: 'Media-library organization',
  episode_identification: 'Episode identification',
  full_pipeline: 'Complete pipeline',
};

const SetupHealthView = ({ onOpenSettings }: SetupHealthViewProps) => {
  const [health, setHealth] = useState<SystemHealth | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const response = await fetch('/system/health', { cache: 'no-store' });
      if (!response.ok) throw new Error('The setup check could not be completed.');
      setHealth(await response.json());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'The setup check could not be completed.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="mx-auto max-w-5xl space-y-6 animate-fade-in">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold heading-gradient">Setup &amp; Health</h2>
          <p className="mt-2 text-sm text-[var(--text-muted)]">
            RipWeaver checks only whether configured tools and folders are available. It never launches an external tool during this check.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" className="btn btn-secondary" onClick={onOpenSettings}>Open Settings</button>
          <button type="button" className="btn btn-primary" disabled={loading} onClick={() => void refresh()}>
            {loading ? 'Checking…' : 'Recheck setup'}
          </button>
        </div>
      </div>

      {error && <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-red-100">{error}</div>}

      {health && (
        <>
          <div className={`rounded-2xl border p-5 ${health.status === 'ready' ? 'border-green-500/30 bg-green-500/10' : 'border-amber-500/30 bg-amber-500/10'}`}>
            <div className="text-lg font-bold text-white">{health.status === 'ready' ? 'Ready for the complete workflow' : 'RipWeaver can run, but setup needs attention'}</div>
            <p className="mt-1 text-sm text-[var(--text-muted)]">{health.summary}</p>
            <div className="mt-4 flex flex-wrap gap-2">
              {Object.entries(health.ready_for)
                .filter(([key]) => key !== 'launch')
                .map(([key, ready]) => (
                  <span key={key} className={`rounded-full border px-3 py-1 text-xs font-semibold ${ready ? 'border-green-500/30 bg-green-500/10 text-green-200' : 'border-amber-500/30 bg-amber-500/10 text-amber-100'}`}>
                    {ready ? 'Ready' : 'Needs setup'}: {featureLabels[key] || key.replaceAll('_', ' ')}
                  </span>
                ))}
            </div>
          </div>

          {(Object.keys(categoryTitles) as HealthItem['category'][]).map((category) => {
            const items = health.items.filter((item) => item.category === category);
            if (items.length === 0) return null;
            return (
              <section key={category} className="glass-panel rounded-2xl p-5">
                <h3 className="text-xl font-semibold text-white">{categoryTitles[category]}</h3>
                <div className="mt-4 grid gap-3 md:grid-cols-2">
                  {items.map((item) => (
                    <div key={item.id} className={`rounded-xl border p-4 ${statusStyles[item.status]}`}>
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <div className="font-bold text-white">{item.label}</div>
                          <div className="mt-1 text-xs opacity-80">Used for: {item.feature}</div>
                        </div>
                        <span className="rounded-full border border-current/30 px-2 py-1 text-[10px] font-bold uppercase">
                          {item.status === 'available' ? 'Detected' : item.status}
                        </span>
                      </div>
                      <p className="mt-3 text-sm leading-relaxed">{item.message}</p>
                      <div className="mt-4 flex flex-wrap gap-2">
                        {(item.category === 'provider' || item.status === 'missing' || item.status === 'invalid' || item.status === 'available') && (
                          <button type="button" className="btn btn-secondary text-xs" onClick={onOpenSettings}>
                            {item.category === 'provider' ? 'Open credential settings' : 'Fix in Settings'}
                          </button>
                        )}
                        {item.download_url && (
                          <a className="btn btn-primary text-xs" href={item.download_url} target="_blank" rel="noopener noreferrer">
                            {item.category === 'provider' ? 'Get or manage API key' : 'Official download page'}
                          </a>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            );
          })}

          <div className="rounded-xl border border-blue-500/25 bg-blue-500/10 p-4 text-sm text-blue-100">
            RipWeaver does not download or install MakeMKV, HandBrake, FFmpeg, Tesseract, or DiscImageCreator. Download buttons open each project’s official page, and you remain in control of its installer and license.
          </div>
        </>
      )}
    </div>
  );
};

export default SetupHealthView;
