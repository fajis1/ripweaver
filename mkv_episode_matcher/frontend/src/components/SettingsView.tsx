import React, { useState, useEffect } from 'react';

interface Config {
    cache_dir: string;
    min_confidence: number;
    asr_provider: string;
    sub_provider: 'local' | 'opensubtitles';
    open_subtitles_username?: string;
    open_subtitles_password?: string;
    open_subtitles_api_key?: string;
    tmdb_api_key?: string;
    gemini_primary_api_key?: string;
    gemini_paid_api_key?: string;
    rip_output_root?: string;
    transcode_output_root?: string;
    jellyfin_tv_root?: string;
    jellyfin_movie_root?: string;
    makemkv_path?: string;
    handbrake_path?: string;
    ffmpeg_path?: string;
    ffprobe_path?: string;
    default_handbrake_profile: string;
    automatic_processing_enabled: boolean;
    credential_status?: Record<string, {
        configured: boolean;
        management_url: string;
    }>;
}

const SettingsView: React.FC = () => {
    const [config, setConfig] = useState<Config | null>(null);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [message, setMessage] = useState<{ text: string, type: 'success' | 'error' } | null>(null);

    useEffect(() => {
        fetchConfig();
    }, []);

    const fetchConfig = async () => {
        try {
            const res = await fetch('/system/config');
            if (!res.ok) throw new Error('Failed to load config');
            const data = await res.json();
            setConfig(data);
        } catch (err) {
            console.error(err);
            setMessage({ text: 'Failed to load configuration', type: 'error' });
        } finally {
            setLoading(false);
        }
    };

    const handleSave = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!config) return;

        setSaving(true);
        setMessage(null);
        try {
            const res = await fetch('/system/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config),
            });
            const data = await res.json();
            if (data.status === 'success') {
                setConfig(data.config);
                setMessage({ text: 'Settings saved successfully', type: 'success' });
            } else {
                throw new Error(data.message);
            }
        } catch (err) {
            console.error(err);
            setMessage({ text: 'Failed to save settings: ' + String(err), type: 'error' });
        } finally {
            setSaving(false);
        }
    };

    const handleChange = (field: keyof Config, value: string | number | boolean) => {
        if (!config) return;
        setConfig({ ...config, [field]: value });
    };

    if (loading) return <div className="p-8 text-center text-muted">Loading settings...</div>;
    if (!config) return <div className="p-8 text-center text-red-400">Error loading settings</div>;

    return (
        <div className="max-w-4xl mx-auto glass-panel p-8 rounded-2xl animate-fade-in h-full overflow-y-auto">
            <h2 className="text-3xl font-bold mb-8 heading-gradient">System Configuration</h2>

            {message && (
                <div className={`mb-6 p-4 rounded-xl border ${message.type === 'success' ? 'bg-green-500/10 border-green-500/20 text-green-400' : 'bg-red-500/10 border-red-500/20 text-red-400'
                    }`}>
                    {message.text}
                </div>
            )}

            <form onSubmit={handleSave} className="space-y-8">
                {/* Core Settings */}
                <div className="space-y-4">
                    <h3 className="text-xl font-semibold text-white border-b border-[var(--border-color)] pb-2">Core Settings</h3>

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                        <div className="space-y-2">
                            <label className="text-sm font-medium text-muted">Cache Directory</label>
                            <input
                                type="text"
                                value={config.cache_dir}
                                onChange={(e) => handleChange('cache_dir', e.target.value)}
                                className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-4 py-2 text-white focus:outline-none focus:border-[var(--accent-primary)]"
                            />
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 p-4 bg-[var(--bg-tertiary)]/50 rounded-xl border border-[var(--border-color)]">
                            {[
                                ['gemini_primary_api_key', 'gemini-primary', 'Gemini primary API key'],
                                ['gemini_paid_api_key', 'gemini-paid', 'Gemini paid/fallback API key'],
                            ].map(([field, credential, label]) => (
                                <label key={field} className="space-y-2">
                                    <span className="text-sm font-medium text-muted">{label}</span>
                                    <input type="password" value={(config[field as keyof Config] as string) || ''} onChange={(event) => handleChange(field as keyof Config, event.target.value)} placeholder={config.credential_status?.[credential]?.configured ? 'Configured — paste only to replace' : 'Not configured'} className="w-full bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-lg px-4 py-2 text-white" />
                                    <a href={config.credential_status?.[credential]?.management_url || 'https://aistudio.google.com/app/apikey'} target="_blank" rel="noopener noreferrer" className="inline-block text-sm text-indigo-300 underline hover:text-white">Get or manage key</a>
                                </label>
                            ))}
                        </div>

                        <div className="space-y-2">
                            <label className="text-sm font-medium text-muted">Confidence Threshold (0.0 - 1.0)</label>
                            <input
                                type="number"
                                step="0.05"
                                min="0.1"
                                max="1.0"
                                value={config.min_confidence}
                                onChange={(e) => handleChange('min_confidence', parseFloat(e.target.value))}
                                className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-4 py-2 text-white focus:outline-none focus:border-[var(--accent-primary)]"
                            />
                        </div>
                    </div>
                </div>

                <div className="space-y-4">
                    <h3 className="text-xl font-semibold text-white border-b border-[var(--border-color)] pb-2">Media Pipeline Locations</h3>
                    <p className="text-sm text-[var(--text-muted)]">Rips and encodes stay in staging. Verified outputs are placed into Jellyfin only after matching, collision review, and successful verification.</p>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        {[
                            ['rip_output_root', 'MakeMKV rip staging root'],
                            ['transcode_output_root', 'Encoded staging root'],
                            ['jellyfin_tv_root', 'Jellyfin TV library root'],
                            ['jellyfin_movie_root', 'Jellyfin movie library root'],
                        ].map(([field, label]) => (
                            <label key={field} className="space-y-2">
                                <span className="text-sm font-medium text-muted">{label}</span>
                                <input type="text" value={(config[field as keyof Config] as string) || ''} onChange={(event) => handleChange(field as keyof Config, event.target.value)} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-4 py-2 text-white" />
                            </label>
                        ))}
                    </div>
                </div>

                <div className="space-y-4">
                    <h3 className="text-xl font-semibold text-white border-b border-[var(--border-color)] pb-2">External Tools and Defaults</h3>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        {[
                            ['makemkv_path', 'makemkvcon executable'],
                            ['handbrake_path', 'HandBrakeCLI executable'],
                            ['ffmpeg_path', 'FFmpeg executable'],
                            ['ffprobe_path', 'FFprobe executable'],
                        ].map(([field, label]) => (
                            <label key={field} className="space-y-2">
                                <span className="text-sm font-medium text-muted">{label}</span>
                                <input type="text" value={(config[field as keyof Config] as string) || ''} onChange={(event) => handleChange(field as keyof Config, event.target.value)} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-4 py-2 text-white" />
                            </label>
                        ))}
                        <label className="space-y-2">
                            <span className="text-sm font-medium text-muted">Default HandBrake profile</span>
                            <input type="text" value={config.default_handbrake_profile} onChange={(event) => handleChange('default_handbrake_profile', event.target.value)} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-4 py-2 text-white" />
                        </label>
                    </div>
                    <label className="block rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-100">
                        <input type="checkbox" className="mr-3" checked={config.automatic_processing_enabled} onChange={(event) => handleChange('automatic_processing_enabled', event.target.checked)} />
                        Automatically process inserted discs when the background watcher is installed and attached. This does not authorize overwrite, deletion, replacement, or ejection.
                    </label>
                </div>

                {/* Integration Settings */}
                <div className="space-y-4">
                    <h3 className="text-xl font-semibold text-white border-b border-[var(--border-color)] pb-2">Integrations</h3>

                    <div className="space-y-4">
                        <div className="space-y-2">
                            <label className="text-sm font-medium text-muted">Subtitle Provider</label>
                            <select
                                value={config.sub_provider}
                                onChange={(e) => handleChange('sub_provider', e.target.value as 'local' | 'opensubtitles')}
                                className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-4 py-2 text-white focus:outline-none focus:border-[var(--accent-primary)]"
                            >
                                <option value="local">Local Only</option>
                                <option value="opensubtitles">OpenSubtitles.com</option>
                            </select>
                        </div>

                        {config.sub_provider === 'opensubtitles' && (
                            <div className="p-4 bg-[var(--bg-tertiary)]/50 rounded-xl space-y-4 border border-[var(--border-color)]">
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                    <div className="space-y-2">
                                        <label className="text-sm font-medium text-muted">Username</label>
                                        <input
                                            type="text"
                                            value={config.open_subtitles_username || ''}
                                            onChange={(e) => handleChange('open_subtitles_username', e.target.value)}
                                            className="w-full bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-lg px-4 py-2 text-white focus:outline-none focus:border-[var(--accent-primary)]"
                                        />
                                    </div>
                                    <div className="space-y-2">
                                        <label className="text-sm font-medium text-muted">Password</label>
                                        <input
                                            type="password"
                                            value={config.open_subtitles_password || ''}
                                            onChange={(e) => handleChange('open_subtitles_password', e.target.value)}
                                            placeholder={config.credential_status?.['opensubtitles-password']?.configured ? 'Configured — paste only to replace' : 'Not configured'}
                                            className="w-full bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-lg px-4 py-2 text-white focus:outline-none focus:border-[var(--accent-primary)]"
                                        />
                                    </div>
                                    <div className="col-span-full space-y-2">
                                        <label className="text-sm font-medium text-muted">API Key</label>
                                        <input
                                            type="password"
                                            value={config.open_subtitles_api_key || ''}
                                            onChange={(e) => handleChange('open_subtitles_api_key', e.target.value)}
                                            placeholder={config.credential_status?.['opensubtitles-api']?.configured ? 'Configured — paste only to replace' : 'Not configured'}
                                            className="w-full bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-lg px-4 py-2 text-white focus:outline-none focus:border-[var(--accent-primary)]"
                                        />
                                        <a
                                            href={config.credential_status?.['opensubtitles-api']?.management_url || 'https://www.opensubtitles.com/en/consumers'}
                                            target="_blank"
                                            rel="noopener noreferrer"
                                            className="inline-block text-sm text-indigo-300 underline hover:text-white"
                                        >
                                            Get or manage an OpenSubtitles API key
                                        </a>
                                    </div>
                                </div>
                            </div>
                        )}

                        <div className="space-y-2">
                            <label className="text-sm font-medium text-muted">TMDb API Key (Optional)</label>
                            <input
                                type="password"
                                value={config.tmdb_api_key || ''}
                                onChange={(e) => handleChange('tmdb_api_key', e.target.value)}
                                className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-4 py-2 text-white focus:outline-none focus:border-[var(--accent-primary)]"
                                placeholder={config.credential_status?.tmdb?.configured ? 'Configured — paste only to replace' : 'Not configured (optional)'}
                            />
                            <a
                                href={config.credential_status?.tmdb?.management_url || 'https://www.themoviedb.org/settings/api'}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="inline-block text-sm text-indigo-300 underline hover:text-white"
                            >
                                Get or manage a TMDb API key
                            </a>
                        </div>
                    </div>
                </div>

                <div className="pt-4 flex justify-end">
                    <button
                        type="submit"
                        disabled={saving}
                        className={`btn btn-primary px-8 py-3 text-lg shadow-lg shadow-blue-500/20 ${saving ? 'opacity-70 cursor-wait' : ''}`}
                    >
                        {saving ? 'Saving...' : 'Save Configuration'}
                    </button>
                </div>
            </form>
        </div>
    );
};

export default SettingsView;
