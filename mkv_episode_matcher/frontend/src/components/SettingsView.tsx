import React, { useState, useEffect } from 'react';

interface Config {
    cache_dir: string;
    min_confidence: number;
    gemini_model: string;
    gemini_fallback_models: string[];
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
    deletion_staging_root?: string;
    retained_source_ttl_days: number;
    jellyfin_tv_root?: string;
    jellyfin_movie_root?: string;
    makemkv_path?: string;
    handbrake_path?: string;
    ffmpeg_path?: string;
    ffprobe_path?: string;
    tesseract_path?: string;
    default_handbrake_profile: string;
    default_handbrake_profile_480p?: string;
    default_handbrake_profile_720p?: string;
    default_handbrake_profile_1080p?: string;
    default_handbrake_profile_2160p?: string;
    remember_last_handbrake_profile: boolean;
    automatic_processing_enabled: boolean;
    automatic_eject_after_rip: boolean;
    automatic_gemini_ambiguity_fallback: boolean;
    automatic_organization_enabled: boolean;
    thediscdb_lookup_enabled: boolean;
    ripweaver_catalogue_enabled: boolean;
    ripweaver_catalogue_contributions_enabled: boolean;
    ripweaver_catalogue_url: string;
    credential_status?: Record<string, {
        configured: boolean;
        last4?: string | null;
        management_url: string;
    }>;
}

interface CatalogueStatus {
    enabled: boolean;
    connected: boolean;
    compatible: boolean | null;
    registered: boolean;
    contributions_enabled: boolean;
    contribution_outbox: {
        snapshots: number;
        pending: number;
        sent: number;
        superseded: number;
    } | null;
    capabilities: {
        schema_version: number;
        service_version: string;
        automatic_piecewise_consensus: boolean;
        provisional_help: boolean;
        independent_quorum: number;
        support_checkout: boolean;
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

const CREDENTIAL_LABELS: Record<string, string> = {
    tmdb: 'TMDb API key',
    'opensubtitles-api': 'OpenSubtitles API key',
    'opensubtitles-username': 'OpenSubtitles username',
    'opensubtitles-password': 'OpenSubtitles password',
    'gemini-primary': 'Gemini primary API key',
    'gemini-paid': 'Gemini backup API key',
    'ripweaver-catalogue': 'RipWeaver Catalogue installation',
};

const CREDENTIAL_SETTING_TARGETS: Record<string, string> = {
    tmdb: 'settings-tmdb-api-key',
    'opensubtitles-api': 'settings-opensubtitles-api-key',
    'opensubtitles-username': 'settings-opensubtitles-username',
    'opensubtitles-password': 'settings-opensubtitles-password',
    'gemini-primary': 'settings-gemini-primary-api-key',
    'gemini-paid': 'settings-gemini-backup-api-key',
    'ripweaver-catalogue': 'settings-ripweaver-catalogue',
};

interface StoredProfile {
    profile_id: string;
    display_name: string;
    built_in: boolean;
    profile: {
        encoder: string;
        encoder_preset: string;
        quality: number;
        quality_480p: number;
        quality_720p: number;
        quality_1080p: number;
        quality_2160p: number;
        selective_decomb: boolean;
        content_kind: string;
        nlmeans_preset: string | null;
        nlmeans_tune: string;
        audio_track: number;
        audio_preference: string;
        audio_primary_layout: string;
        audio_secondary_layout: string;
        audio_default_language: string;
        audio_language: string;
        audio_selection: string;
        additional_audio: string;
        subtitle_language: string;
        subtitle_selection: string;
        subtitle_default: string;
        resolution_policy: string;
        frame_rate_policy: string;
        compatibility_audio_bitrate: number;
        audio_bitrate_stereo: number;
        audio_bitrate_2_1: number;
        audio_bitrate_5_1: number;
        audio_bitrate_7_1: number;
        stereo_first: boolean;
        retain_subtitles: boolean;
    };
}

interface FolderPickerState {
    field: keyof Config;
    label: string;
    current: string | null;
    parent: string | null;
    entries: Array<{ name: string; path: string }>;
}

const AUDIO_LANGUAGE_OPTIONS = [
    ['default', 'Source/default'], ['eng', 'English'], ['spa', 'Spanish'], ['fre', 'French'], ['deu', 'German'],
    ['ita', 'Italian'], ['por', 'Portuguese'], ['jpn', 'Japanese'], ['kor', 'Korean'], ['zho', 'Chinese'],
    ['rus', 'Russian'], ['ara', 'Arabic'], ['hin', 'Hindi'], ['nld', 'Dutch'], ['pol', 'Polish'],
    ['dan', 'Danish'], ['fin', 'Finnish'], ['nor', 'Norwegian'], ['swe', 'Swedish'], ['tur', 'Turkish'],
    ['heb', 'Hebrew'], ['ces', 'Czech'], ['hun', 'Hungarian'], ['ron', 'Romanian'], ['ukr', 'Ukrainian'],
    ['gre', 'Greek'], ['tha', 'Thai'], ['vie', 'Vietnamese'], ['ind', 'Indonesian'], ['may', 'Malay'],
    ['ell', 'Modern Greek'], ['lat', 'Latin'], ['und', 'Unknown / undefined'],
];

const GEMINI_MODEL_OPTIONS = [
    { value: 'gemini-3.7-flash', label: 'Gemini 3.7 Flash (Recommended)' },
    { value: 'gemini-3.6-flash', label: 'Gemini 3.6 Flash' },
    { value: 'gemini-3.1-flash-lite', label: 'Gemini 3.1 Flash-Lite (Recommended lightweight)' },
    { value: 'gemini-3.5-flash-lite', label: 'Gemini 3.5 Flash-Lite (Newer lightweight)' },
    { value: 'gemini-3.5-flash', label: 'Gemini 3.5 Flash' },
    { value: 'gemini-2.5-flash', label: 'Gemini 2.5 Flash' },
    { value: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro (Most accurate)' },
    { value: 'gemini-2.5-flash-lite', label: 'Gemini 2.5 Flash-Lite' },
    { value: 'gemma-4-31b-it', label: 'Gemma 4 31B IT (Free tier / open weights)' },
];

const SettingsView: React.FC = () => {
    const [config, setConfig] = useState<Config | null>(null);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [message, setMessage] = useState<{ text: string, type: 'success' | 'error' } | null>(null);
    const [toolErrors, setToolErrors] = useState<Record<string, string>>({});
    const [profiles, setProfiles] = useState<StoredProfile[]>([]);
    const [folderPicker, setFolderPicker] = useState<FolderPickerState | null>(null);
    const [audioChoicePrompt, setAudioChoicePrompt] = useState(false);
    const [audioPromptLanguage, setAudioPromptLanguage] = useState('eng');
    const [languageMultiSelectHint, setLanguageMultiSelectHint] = useState(false);
    const [audioModeWarning, setAudioModeWarning] = useState<string | null>(null);
    const [subtitleLanguageHint, setSubtitleLanguageHint] = useState(false);
    const [profileSaveResult, setProfileSaveResult] = useState<{ text: string, type: 'success' | 'error' } | null>(null);
    const [catalogueStatus, setCatalogueStatus] = useState<CatalogueStatus | null>(null);
    const [catalogueStatusError, setCatalogueStatusError] = useState<string | null>(null);
    const [connectingCatalogue, setConnectingCatalogue] = useState(false);
    const [pendingCredentialTarget, setPendingCredentialTarget] = useState<string | null>(null);
    const [profileDraft, setProfileDraft] = useState({
        profile_id: '', display_name: '', encoder: 'vce_h265', encoder_preset: 'quality', quality: 24, quality_480p: 26, quality_720p: 25, quality_1080p: 24, quality_2160p: 22,
        selective_decomb: true, content_kind: 'unknown', nlmeans_preset: '', nlmeans_tune: 'none',
        audio_preference: 'default', audio_primary_layout: 'stereo', audio_secondary_layout: 'highest', audio_default_language: 'default', audio_language: 'default', audio_selection: 'all_matching', additional_audio: 'selected_only', subtitle_language: 'eng', subtitle_selection: 'all_matching', subtitle_default: 'none', resolution_policy: 'source', frame_rate_policy: 'source', compatibility_audio_bitrate: 256, audio_bitrate_stereo: 256, audio_bitrate_2_1: 320, audio_bitrate_5_1: 512, audio_bitrate_7_1: 640, stereo_first: true, retain_subtitles: true,
    });

    useEffect(() => {
        fetchConfig();
        void fetchCatalogueStatus();
        fetch('/rip/handbrake/profiles').then((response) => response.ok ? response.json() : null).then((payload) => {
            if (payload?.profiles) setProfiles(payload.profiles);
        }).catch(() => undefined);
    }, []);

    useEffect(() => {
        if (!pendingCredentialTarget) return;
        const targetId = CREDENTIAL_SETTING_TARGETS[pendingCredentialTarget];
        const target = targetId ? document.getElementById(targetId) : null;
        if (!target) return;
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        target.focus({ preventScroll: true });
        setPendingCredentialTarget(null);
    }, [pendingCredentialTarget, config?.sub_provider]);

    const openCredentialSetting = (name: string) => {
        if (!config || !CREDENTIAL_SETTING_TARGETS[name]) return;
        if (name.startsWith('opensubtitles-') && config.sub_provider !== 'opensubtitles') {
            setConfig({ ...config, sub_provider: 'opensubtitles' });
        }
        setPendingCredentialTarget(name);
    };

    const editProfile = (item: StoredProfile, duplicate: boolean) => {
        setProfileDraft({
            ...item.profile,
            audio_primary_layout: item.profile.audio_primary_layout || (item.profile.stereo_first ? 'stereo' : item.profile.audio_preference),
            audio_secondary_layout: item.profile.audio_secondary_layout || (item.profile.stereo_first ? item.profile.audio_preference : 'stereo'),
            audio_bitrate_stereo: item.profile.audio_bitrate_stereo ?? 256,
            audio_bitrate_2_1: item.profile.audio_bitrate_2_1 ?? 320,
            audio_bitrate_5_1: item.profile.audio_bitrate_5_1 ?? 512,
            audio_bitrate_7_1: item.profile.audio_bitrate_7_1 ?? 640,
            audio_default_language: item.profile.audio_default_language || 'default',
            profile_id: duplicate ? `${item.profile_id}-custom` : item.profile_id,
            display_name: duplicate ? `${item.display_name} Custom` : item.display_name,
            nlmeans_preset: item.profile.nlmeans_preset || '',
        });
        setMessage({ text: duplicate ? `Copied ${item.display_name} as a starting point.` : `Loaded ${item.display_name}.`, type: 'success' });
    };

    const chooseDefaultProfile = async (item: StoredProfile, scope = 'general') => {
        try {
            const response = await fetch('/rip/handbrake/profiles/default', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ profile_id: item.profile_id, scope }),
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Default profile was not saved.');
            const field = scope === 'general' ? 'default_handbrake_profile' : `default_handbrake_profile_${scope}`;
            setConfig((current) => current ? { ...current, [field]: item.profile_id } : current);
            setMessage({ type: 'success', text: `${item.display_name} is now the ${scope === 'general' ? 'general' : scope} default HandBrake profile.` });
        } catch (error) {
            const message = error instanceof Error ? error.message : 'Default profile was not saved.';
            setMessage({ type: 'error', text: message });
            window.alert(message);
        }
    };

    const saveProfile = async () => {
        setMessage(null);
        setProfileSaveResult(null);
        if (!/^[a-z][a-z0-9-]{1,47}$/.test(profileDraft.profile_id)) {
            const result = { text: 'Profile was not saved. Enter the required Profile ID (2–48 lowercase letters, numbers, or hyphens; begin with a letter).', type: 'error' as const };
            setMessage(result);
            setProfileSaveResult(result);
            document.getElementById('handbrake-profile-id')?.focus();
            return;
        }
        if (!profileDraft.display_name.trim()) {
            const result = { text: 'Profile was not saved. Enter the required Display name.', type: 'error' as const };
            setMessage(result);
            setProfileSaveResult(result);
            document.getElementById('handbrake-profile-display-name')?.focus();
            return;
        }
        const response = await fetch('/rip/handbrake/profiles', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                profile_id: profileDraft.profile_id,
                display_name: profileDraft.display_name,
                profile: {
                    encoder: profileDraft.encoder,
                    encoder_preset: profileDraft.encoder_preset,
                    quality: profileDraft.quality,
                    quality_480p: profileDraft.quality_480p,
                    quality_720p: profileDraft.quality_720p,
                    quality_1080p: profileDraft.quality_1080p,
                    quality_2160p: profileDraft.quality_2160p,
                    selective_decomb: profileDraft.selective_decomb,
                    content_kind: profileDraft.content_kind,
                    nlmeans_preset: profileDraft.nlmeans_preset || null,
                    nlmeans_tune: profileDraft.nlmeans_preset ? profileDraft.nlmeans_tune : 'none',
                    audio_track: 1,
                    audio_preference: profileDraft.audio_preference,
                    audio_primary_layout: profileDraft.audio_primary_layout,
                    audio_secondary_layout: profileDraft.audio_secondary_layout,
                    audio_default_language: profileDraft.audio_default_language,
                    audio_language: profileDraft.audio_language,
                    audio_selection: profileDraft.audio_selection,
                    additional_audio: profileDraft.additional_audio,
                    subtitle_language: profileDraft.subtitle_language,
                    subtitle_selection: profileDraft.subtitle_selection,
                    subtitle_default: profileDraft.subtitle_default,
                    resolution_policy: profileDraft.resolution_policy,
                    frame_rate_policy: profileDraft.frame_rate_policy,
                    compatibility_audio_bitrate: profileDraft.compatibility_audio_bitrate,
                    audio_bitrate_stereo: profileDraft.audio_bitrate_stereo,
                    audio_bitrate_2_1: profileDraft.audio_bitrate_2_1,
                    audio_bitrate_5_1: profileDraft.audio_bitrate_5_1,
                    audio_bitrate_7_1: profileDraft.audio_bitrate_7_1,
                    stereo_first: profileDraft.stereo_first,
                    retain_subtitles: profileDraft.retain_subtitles,
                },
            }),
        });
        const payload = await response.json();
        if (!response.ok) {
            const result = { text: typeof payload.detail === 'string' ? `Profile was not saved. ${payload.detail}` : 'Profile was not saved. The server rejected one or more settings.', type: 'error' as const };
            setMessage(result);
            setProfileSaveResult(result);
            return;
        }
        setProfiles((current) => [...current.filter((item) => item.profile_id !== payload.profile_id), payload]);
        const result = { text: `HandBrake profile “${payload.display_name}” saved and added to the profile list.`, type: 'success' as const };
        setMessage(result);
        setProfileSaveResult(result);
    };

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

    const fetchCatalogueStatus = async () => {
        setCatalogueStatusError(null);
        try {
            const response = await fetch('/catalogue/status');
            const payload = await response.json();
            if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Connection status is unavailable.');
            setCatalogueStatus(payload as CatalogueStatus);
        } catch (error) {
            setCatalogueStatus(null);
            setCatalogueStatusError(error instanceof Error ? error.message : 'Connection status is unavailable.');
        }
    };

    const connectCatalogue = async () => {
        setConnectingCatalogue(true);
        setCatalogueStatusError(null);
        try {
            const response = await fetch('/catalogue/register', { method: 'POST' });
            const payload = await response.json();
            if (!response.ok || payload.registered !== true) {
                throw new Error(typeof payload.detail === 'string' ? payload.detail : 'This installation could not be registered.');
            }
            await Promise.all([fetchCatalogueStatus(), fetchConfig()]);
            setMessage({ text: 'This RipWeaver installation is connected to the community catalogue.', type: 'success' });
        } catch (error) {
            const detail = error instanceof Error ? error.message : 'This installation could not be registered.';
            setCatalogueStatusError(detail);
            setMessage({ text: detail, type: 'error' });
        } finally {
            setConnectingCatalogue(false);
        }
    };

    const discoverTools = async () => {
        try {
            const res = await fetch('/system/tools/discover');
            if (!res.ok) throw new Error('Tool discovery failed');
            const data = await res.json();
            const found = data.tools || {};
            if (!config) return;
            setConfig({ ...config, ...Object.fromEntries(Object.entries(found).filter(([, value]) => value)) });
            setMessage({ text: 'Detected installed tools. Save Configuration to keep these paths.', type: 'success' });
        } catch (err) {
            setMessage({ text: 'Could not detect installed tools: ' + String(err), type: 'error' });
        }
    };

    const openFolderPicker = async (field: keyof Config, label: string, path?: string) => {
        try {
            setMessage(null);
            const query = path ? `?path=${encodeURIComponent(path)}` : '';
            const res = await fetch(`/system/folders${query}`);
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Folder could not be read');
            setFolderPicker({ field, label, current: data.current, parent: data.parent, entries: data.entries || [] });
        } catch (err) {
            setMessage({ text: 'Could not browse folders: ' + String(err), type: 'error' });
        }
    };

    const handleSave = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!config) return;

        setSaving(true);
        setMessage(null);
        setToolErrors({});
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
                void fetchCatalogueStatus();
            } else {
                if (data.field_errors && typeof data.field_errors === 'object') setToolErrors(data.field_errors);
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
        if (toolErrors[field]) setToolErrors((current) => Object.fromEntries(Object.entries(current).filter(([key]) => key !== field)));
        setConfig({ ...config, [field]: value });
    };

    const handleGeminiFallbackChange = (index: number, value: string) => {
        if (!config) return;
        const models = [...(config.gemini_fallback_models || [])];
        models[index] = value;
        setConfig({
            ...config,
            gemini_fallback_models: models.filter((model) => model.trim()),
        });
    };

    if (loading) return <div className="p-8 text-center text-muted">Loading settings...</div>;
    if (!config) return <div className="p-8 text-center text-red-400">Error loading settings</div>;

    return (
        <div className="max-w-4xl mx-auto glass-panel p-8 rounded-2xl animate-fade-in h-full overflow-y-auto">
            <h2 className="text-3xl font-bold mb-8 heading-gradient">System Configuration</h2>

            {folderPicker && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" role="dialog" aria-modal="true" aria-label={`Choose ${folderPicker.label}`} onMouseDown={(event) => { if (event.currentTarget === event.target) setFolderPicker(null); }}>
                  <div className="w-full max-w-3xl rounded-xl border border-[var(--border-color)] bg-[var(--bg-secondary)] p-5 shadow-2xl space-y-3">
                    <div className="flex items-center justify-between gap-3">
                        <div>
                            <div className="font-semibold text-white">Choose {folderPicker.label}</div>
                            <div className="text-xs text-[var(--text-muted)] break-all">{folderPicker.current || 'This PC'}</div>
                        </div>
                        <button type="button" className="btn btn-secondary" onClick={() => setFolderPicker(null)}>Cancel</button>
                    </div>
                    <div className="flex gap-2">
                        {folderPicker.parent && <button type="button" className="btn btn-secondary" onClick={() => openFolderPicker(folderPicker.field, folderPicker.label, folderPicker.parent || undefined)}>Up</button>}
                        {folderPicker.current && <button type="button" className="btn btn-primary" onClick={() => { handleChange(folderPicker.field, folderPicker.current || ''); setFolderPicker(null); }}>Choose this folder</button>}
                    </div>
                    <div className="max-h-64 overflow-y-auto space-y-1">
                        {folderPicker.entries.map((entry) => <button type="button" key={entry.path} className="block w-full text-left rounded-lg px-3 py-2 text-sm text-white hover:bg-[var(--bg-primary)]" onClick={() => openFolderPicker(folderPicker.field, folderPicker.label, entry.path)}>📁 {entry.name}</button>)}
                        {folderPicker.entries.length === 0 && <div className="text-sm text-[var(--text-muted)]">No readable subfolders.</div>}
                    </div>
                  </div>
                </div>
            )}

            {audioChoicePrompt && (
                <div className="hidden">
                    <div className="font-semibold text-white">Choose additional audio behavior</div>
                    <p className="text-sm text-[var(--text-muted)]">You checked “Keep other source audio streams.” Do you want every language/commentary track, or only your default language?</p>
                    <label className="block space-y-1">
                        <span className="text-xs text-[var(--text-muted)]">Default audio language</span>
                        <select value={profileDraft.audio_language} onChange={(event) => setProfileDraft({ ...profileDraft, audio_language: event.target.value })} className="w-full bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white">
                            {AUDIO_LANGUAGE_OPTIONS.filter(([code]) => code !== 'default').map(([code, name]) => <option key={code} value={code}>{name} ({code})</option>)}
                        </select>
                    </label>
                    <div className="flex flex-wrap gap-2">
                        <button type="button" className="btn btn-primary" onClick={() => { setProfileDraft({ ...profileDraft, additional_audio: 'selected_only' }); setAudioChoicePrompt(false); }}>Keep only this language</button>
                        <button type="button" className="btn btn-secondary" onClick={() => { setProfileDraft({ ...profileDraft, additional_audio: 'all' }); setAudioChoicePrompt(false); }}>Keep all source languages</button>
                        <button type="button" className="btn btn-secondary" onClick={() => { setProfileDraft({ ...profileDraft, additional_audio: 'selected_only' }); setAudioChoicePrompt(false); }}>Cancel</button>
                    </div>
                </div>
            )}

            {message && (
                <div role="status" aria-live="polite" className={`fixed right-6 top-6 z-[100] flex max-w-md items-start gap-4 rounded-xl border p-4 shadow-2xl backdrop-blur ${message.type === 'success' ? 'bg-green-950/95 border-green-400/50 text-green-100' : 'bg-red-950/95 border-red-400/50 text-red-100'
                    }`}>
                    <span className="text-xl" aria-hidden="true">{message.type === 'success' ? '✓' : '⚠'}</span>
                    <div className="flex-1"><div className="font-semibold">{message.type === 'success' ? 'Saved successfully' : 'Action needs attention'}</div><div className="mt-1 text-sm">{message.text}</div></div>
                    <button type="button" className="text-xl leading-none opacity-70 hover:opacity-100" aria-label="Dismiss notification" onClick={() => setMessage(null)}>×</button>
                </div>
            )}

            <form onSubmit={handleSave} className="space-y-8">
                {/* Core Settings */}
                <div className="space-y-4">
                    <h3 className="text-xl font-semibold text-white border-b border-[var(--border-color)] pb-2">Core Settings</h3>

                    <div className="rounded-xl border border-[var(--border-color)] p-4 space-y-3">
                        <div className="font-semibold text-white">Credential status</div>
                        <p className="text-xs text-[var(--text-muted)]">Only whether each local credential is configured and its final four characters are shown. Full values are never returned by the server. Select a credential to jump to its field, paste or replace the value, and then save Settings.</p>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                            {Object.entries(config.credential_status || {}).map(([name, status]) => (
                                <button
                                    key={name}
                                    type="button"
                                    onClick={() => openCredentialSetting(name)}
                                    className="group flex items-center justify-between gap-3 rounded-lg border border-transparent bg-[var(--bg-tertiary)]/50 px-3 py-2 text-left text-sm transition hover:border-[var(--accent-primary)] hover:bg-[var(--bg-tertiary)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-primary)]"
                                    aria-label={`${name === 'ripweaver-catalogue' ? 'Manage' : status.configured ? 'Replace' : 'Add'} ${CREDENTIAL_LABELS[name] || name}`}
                                >
                                    <span className="min-w-0">
                                        <span className="block truncate text-muted">{CREDENTIAL_LABELS[name] || name}</span>
                                        <span className={status.configured ? 'block text-xs text-green-300' : 'block text-xs text-[var(--text-muted)]'}>
                                            {status.configured ? `Configured${status.last4 ? ` · …${status.last4}` : ''}` : 'Not configured'}
                                        </span>
                                    </span>
                                    <span className="shrink-0 rounded-md bg-indigo-500/15 px-2 py-1 text-xs font-medium text-indigo-200 group-hover:bg-indigo-500/25">
                                        {name === 'ripweaver-catalogue' ? 'Manage' : status.configured ? 'Replace' : 'Add'} →
                                    </span>
                                </button>
                            ))}
                        </div>
                    </div>

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
                                ['gemini_paid_api_key', 'gemini-paid', 'Gemini backup API key'],
                            ].map(([field, credential, label]) => (
                                <label key={field} className="space-y-2">
                                    <span className="text-sm font-medium text-muted">{label}</span>
                                    <input id={CREDENTIAL_SETTING_TARGETS[credential]} type="password" value={(config[field as keyof Config] as string) || ''} onChange={(event) => handleChange(field as keyof Config, event.target.value)} placeholder={config.credential_status?.[credential]?.configured ? 'Configured — paste only to replace' : 'Not configured'} className="w-full bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-lg px-4 py-2 text-white" />
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
                    <div className="rounded-xl border border-indigo-500/30 bg-indigo-500/10 p-4 space-y-4">
                        <div>
                            <h4 className="font-semibold text-indigo-100">Gemini model selection and fallback</h4>
                            <p className="mt-1 text-xs text-indigo-100/70">Choose the primary model and up to two models RipWeaver may try in order when capacity is exhausted.</p>
                        </div>
                        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
                            <label className="space-y-2 rounded-lg border border-indigo-300/15 bg-black/10 p-3">
                                <span className="flex items-center gap-2 text-sm font-medium text-indigo-100"><span className="rounded-full bg-indigo-400/20 px-2 py-0.5 text-xs">1</span>Primary model</span>
                                <select value={config.gemini_model} onChange={(event) => handleChange('gemini_model', event.target.value)} className="w-full rounded-lg border border-indigo-300/25 bg-slate-950 px-3 py-2 text-white focus:outline-none focus:ring-2 focus:ring-indigo-400">
                                    {!GEMINI_MODEL_OPTIONS.some((option) => option.value === config.gemini_model) && (
                                        <option className="bg-slate-950 text-white" value={config.gemini_model}>{config.gemini_model} (saved custom model)</option>
                                    )}
                                    {GEMINI_MODEL_OPTIONS.map((option) => <option className="bg-slate-950 text-white" key={option.value} value={option.value}>{option.label}</option>)}
                                </select>
                            </label>
                            {[0, 1].map((index) => (
                                <label key={index} className="space-y-2 rounded-lg border border-indigo-300/15 bg-black/10 p-3">
                                    <span className="flex items-center gap-2 text-sm font-medium text-indigo-100"><span className="rounded-full bg-indigo-400/20 px-2 py-0.5 text-xs">{index + 2}</span>{index === 0 ? 'First fallback model' : 'Second fallback model'}</span>
                                    <select value={config.gemini_fallback_models?.[index] || ''} onChange={(event) => handleGeminiFallbackChange(index, event.target.value)} className="w-full rounded-lg border border-indigo-300/25 bg-slate-950 px-3 py-2 text-white focus:outline-none focus:ring-2 focus:ring-indigo-400">
                                        <option className="bg-slate-950 text-white" value="">None</option>
                                        {config.gemini_fallback_models?.[index] && !GEMINI_MODEL_OPTIONS.some((option) => option.value === config.gemini_fallback_models[index]) && (
                                            <option className="bg-slate-950 text-white" value={config.gemini_fallback_models[index]}>{config.gemini_fallback_models[index]} (saved custom model)</option>
                                        )}
                                        {GEMINI_MODEL_OPTIONS.map((option) => <option className="bg-slate-950 text-white" key={option.value} value={option.value}>{option.label}</option>)}
                                    </select>
                                </label>
                            ))}
                        </div>
                        <p className="text-xs text-indigo-100/70">
                            Order: primary → first fallback → second fallback. RipWeaver tries both configured API keys for each model before advancing after HTTP 429 capacity exhaustion, sustained HTTP 503 overload, or a definite unavailable-model response. Model IDs are non-secret; API keys remain protected in the local credential file.
                        </p>
                    </div>
                </div>

                <div className="space-y-4">
                    <h3 className="text-xl font-semibold text-white border-b border-[var(--border-color)] pb-2">Media Pipeline Locations</h3>
                    <p className="text-sm text-[var(--text-muted)]">Rips and encodes stay in staging. After verified media-library placement, an original rip can be retained in staging for deletion so it remains available for a later re-encode. The library folders may be used by Plex, Jellyfin, Emby, or another media server.</p>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        {[
                            ['rip_output_root', 'MakeMKV rip staging root'],
                            ['transcode_output_root', 'Encoded staging root'],
                            ['deletion_staging_root', 'Staging for deletion / reprocessing root'],
                            ['jellyfin_tv_root', 'TV media library root'],
                            ['jellyfin_movie_root', 'Movie media library root'],
                        ].map(([field, label]) => (
                            <div key={field} className="space-y-2">
                                <span className="text-sm font-medium text-muted">{label}</span>
                                <input type="text" value={(config[field as keyof Config] as string) || ''} onChange={(event) => handleChange(field as keyof Config, event.target.value)} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-4 py-2 text-white" />
                                <button type="button" className="btn btn-secondary text-xs" onClick={() => openFolderPicker(field as keyof Config, label, (config[field as keyof Config] as string) || undefined)}>Browse folders</button>
                            </div>
                        ))}
                    </div>
                    <div className="space-y-2">
                        <label className="text-sm font-medium text-muted">Retained original TTL (days)</label>
                        <input
                            type="number"
                            min="1"
                            max="3650"
                            step="1"
                            value={config.retained_source_ttl_days}
                            onChange={(event) => handleChange('retained_source_ttl_days', Number(event.target.value))}
                            className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-4 py-2 text-white"
                        />
                        <p className="text-xs text-[var(--text-muted)]">Retained originals stay available in cleanup staging for this many days before RipWeaver treats them as expired.</p>
                    </div>
                </div>

                <div className="space-y-4">
                    <h3 className="text-xl font-semibold text-white border-b border-[var(--border-color)] pb-2">External Tools and Defaults</h3>
                    <p className="text-sm text-[var(--text-muted)]">Use Detect installed tools to find executables on Windows PATH and standard install locations. The tool is not launched during detection.</p>
                    <button type="button" className="btn btn-secondary" onClick={discoverTools}>Detect installed tools</button>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        {[
                            ['makemkv_path', 'makemkvcon executable'],
                            ['handbrake_path', 'HandBrakeCLI executable'],
                            ['ffmpeg_path', 'FFmpeg executable'],
                            ['ffprobe_path', 'FFprobe executable'],
                            ['tesseract_path', 'Tesseract OCR executable'],
                        ].map(([field, label]) => (
                            <label key={field} className="space-y-2">
                                <span className="text-sm font-medium text-muted">{label}</span>
                                <input type="text" value={(config[field as keyof Config] as string) || ''} onChange={(event) => handleChange(field as keyof Config, event.target.value)} className={`w-full bg-[var(--bg-tertiary)] border rounded-lg px-4 py-2 text-white ${toolErrors[field] ? 'border-red-500' : 'border-[var(--border-color)]'}`} />
                                {toolErrors[field] && <span className="block text-xs text-red-400">{toolErrors[field]}</span>}
                            </label>
                        ))}
                        <label className="space-y-2">
                            <span className="text-sm font-medium text-muted">Default HandBrake profile</span>
                            <input type="text" value={config.default_handbrake_profile} onChange={(event) => handleChange('default_handbrake_profile', event.target.value)} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-4 py-2 text-white" />
                            <label className="mt-3 flex items-start gap-3 rounded-lg border border-[var(--border-color)] p-3 text-sm text-white">
                                <input type="checkbox" className="mt-1" checked={config.remember_last_handbrake_profile} onChange={(event) => handleChange('remember_last_handbrake_profile', event.target.checked)} />
                                <span><span className="font-semibold">Remember the last selected profile for future discs</span><span className="mt-1 block text-xs text-[var(--text-muted)]">When enabled, choosing a profile on a disc makes it the default for discs inserted later and after a server restart. Existing per-disc overrides are unchanged.</span></span>
                            </label>
                        </label>
                    </div>
                    <label className="block rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-100">
                        <input type="checkbox" className="mr-3" checked={config.automatic_processing_enabled} onChange={(event) => handleChange('automatic_processing_enabled', event.target.checked)} />
                        Automatically process inserted discs when the background watcher is installed and attached. This does not authorize overwrite, deletion, replacement, or ejection.
                    </label>
                    <label className="block rounded-xl border border-cyan-500/30 bg-cyan-500/10 p-4 text-sm text-cyan-100">
                        <input type="checkbox" className="mr-3" checked={config.automatic_eject_after_rip} onChange={(event) => handleChange('automatic_eject_after_rip', event.target.checked)} />
                        Automatically eject a disc after every reviewed title has ripped and verified, or after a one-minute cancellable countdown when every known destination already exists in the media library. Failure, timeout, pause, stop, unfinished rerip work, or other active rip work prevents ejection.
                    </label>
                    <label className="block rounded-xl border border-indigo-500/30 bg-indigo-500/10 p-4 text-sm text-indigo-100">
                        <input type="checkbox" className="mr-3" checked={config.automatic_gemini_ambiguity_fallback} onChange={(event) => handleChange('automatic_gemini_ambiguity_fallback', event.target.checked)} />
                        Use Gemini as the final fallback for unresolved bonus features after local catalogue, subtitle, OCR, and transcription evidence is exhausted. Each external use remains visible; no MKV, local path, credential, or full transcript is sent.
                    </label>
                    <label className="block rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-4 text-sm text-emerald-100">
                        <input type="checkbox" className="mr-3" checked={config.thediscdb_lookup_enabled} onChange={(event) => handleChange('thediscdb_lookup_enabled', event.target.checked)} />
                        Look up inserted discs in TheDiscDB. RipWeaver reads only disc file names and sizes needed for the database identifier, sends only that identifier to TheDiscDB, and accepts episode names only when the returned source playlist and segment map agree with MakeMKV.
                    </label>
                    <div id="settings-ripweaver-catalogue" tabIndex={-1} className="scroll-mt-6 rounded-xl border border-violet-500/30 bg-violet-500/10 p-4 text-sm text-violet-100 space-y-3 focus:outline-none focus:ring-2 focus:ring-violet-400">
                        <label className="block">
                            <input type="checkbox" className="mr-3" checked={config.ripweaver_catalogue_enabled} onChange={(event) => handleChange('ripweaver_catalogue_enabled', event.target.checked)} />
                            Use the community RipWeaver Catalogue for automatic disc identification. Only the compatibility disc identifier is sent; media, local paths, drive details, and media-library information are never uploaded.
                        </label>
                        <label className="block rounded-lg border border-violet-300/20 bg-black/10 p-3">
                            <input type="checkbox" className="mr-3" checked={config.ripweaver_catalogue_contributions_enabled} onChange={(event) => handleChange('ripweaver_catalogue_contributions_enabled', event.target.checked)} />
                            Automatically contribute cumulative, durably matched title layouts from eligible discs. This is one-time consent for future eligible discs; unresolved titles are omitted until matched. Uploads contain only the disc identifier, playlist/segment structure, runtimes, sizes, match provenance, and canonical media names. No media, local paths, drive identity, media-library location, transcript, or credential is uploaded.
                        </label>
                        <label className="block space-y-1">
                            <span className="text-xs font-semibold">Catalogue server</span>
                            <input type="url" value={config.ripweaver_catalogue_url} onChange={(event) => handleChange('ripweaver_catalogue_url', event.target.value)} className="w-full rounded-lg border border-violet-300/25 bg-[var(--bg-primary)] px-3 py-2 text-white" />
                            <span className="block text-xs text-violet-100/70">The public service uses ten free automatic successful lookups per month, then earned or supported credits. A manual lookup remains available after the visible support prompt.</span>
                        </label>
                        <div className="rounded-lg border border-violet-300/20 bg-black/15 p-3 space-y-2">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                                <div>
                                    <div className="font-semibold text-white">Catalogue connection</div>
                                    <div className="text-xs text-violet-100/70">
                                        {!catalogueStatus
                                            ? catalogueStatusError || 'Checking the saved catalogue settings...'
                                            : !catalogueStatus.enabled
                                              ? 'Disabled in the saved configuration. Enable it above and save before connecting.'
                                              : !catalogueStatus.connected
                                                ? 'The saved server could not be reached.'
                                                : catalogueStatus.compatible === false
                                                  ? 'The server is reachable, but its protocol is not compatible with this desktop version.'
                                                  : catalogueStatus.registered
                                                    ? `Connected and registered${catalogueStatus.capabilities ? ` · schema ${catalogueStatus.capabilities.schema_version}` : ''}`
                                                    : 'Server is reachable and compatible; this installation is not registered yet.'}
                                    </div>
                                </div>
                                <div className="flex flex-wrap gap-2">
                                    <button type="button" className="btn btn-secondary text-xs" onClick={() => void fetchCatalogueStatus()}>Check connection</button>
                                    {catalogueStatus?.enabled && catalogueStatus.connected && catalogueStatus.compatible !== false && !catalogueStatus.registered && (
                                        <button type="button" className="btn btn-primary text-xs" disabled={connectingCatalogue} onClick={() => void connectCatalogue()}>
                                            {connectingCatalogue ? 'Connecting...' : 'Connect this installation'}
                                        </button>
                                    )}
                                </div>
                            </div>
                            {catalogueStatus?.registered && catalogueStatus.usage && (
                                <div className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
                                    <div className="rounded border border-violet-300/15 p-2">
                                        Automatic lookups remaining: <span className="font-semibold text-white">{catalogueStatus.usage.total_automatic_remaining}</span>
                                        <span className="block text-violet-100/60">{catalogueStatus.usage.monthly_remaining} monthly · {catalogueStatus.usage.contribution_credits} contributed · {catalogueStatus.usage.purchased_credits} supported</span>
                                    </div>
                                    {catalogueStatus.contribution_outbox && (
                                        <div className="rounded border border-violet-300/15 p-2">
                                            Contributions: <span className="font-semibold text-white">{catalogueStatus.contribution_outbox.sent} sent</span>
                                            <span className="block text-violet-100/60">{catalogueStatus.contribution_outbox.pending} waiting · {catalogueStatus.contribution_outbox.snapshots} disc snapshots</span>
                                        </div>
                                    )}
                                </div>
                            )}
                            {catalogueStatus?.capabilities?.automatic_piecewise_consensus && (
                                <div className="text-xs text-violet-100/60">
                                    Confirmed titles require {catalogueStatus.capabilities.independent_quorum} independent matching uploads. Unresolved titles remain local while confirmed titles can be reused automatically.
                                </div>
                            )}
                        </div>
                    </div>
                    <label className="block rounded-xl border border-blue-500/30 bg-blue-500/10 p-4 text-sm text-blue-100">
                        <input type="checkbox" className="mr-3" checked={config.automatic_organization_enabled} onChange={(event) => handleChange('automatic_organization_enabled', event.target.checked)} />
                        Automatically move collision-free, verified encodes from staging into the configured media library. Different resolution versions of one episode may coexist; an exact destination or another file at the same resolution stops for review. Overwrite and deletion are never automatic.
                    </label>
                </div>

                <div className="space-y-4">
                    <h3 className="text-xl font-semibold text-white border-b border-[var(--border-color)] pb-2">HandBrake Profiles</h3>
                    <p className="text-sm text-[var(--text-muted)]">Choose H.264, H.265, or AV1 and then select the preferred hardware family. Availability is checked before a transcode begins.</p>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        {profiles.map((item) => {
                            const isDefault = config.default_handbrake_profile === item.profile_id;
                            return <div key={item.profile_id} className={`rounded-lg border p-3 ${isDefault ? 'border-green-400/60 bg-green-500/10' : 'border-[var(--border-color)]'}`}>
                                <label className="flex cursor-pointer items-start gap-3">
                                    <input type="radio" name="default-handbrake-profile" className="mt-1" checked={isDefault} onChange={() => void chooseDefaultProfile(item)} />
                                    <span className="min-w-0"><span className="font-semibold text-white">{item.display_name}</span>{isDefault && <span className="ml-2 rounded-full bg-green-500/20 px-2 py-1 text-[10px] font-bold uppercase text-green-200">Current default</span>}<span className="block text-xs text-[var(--text-muted)]">{item.profile.encoder} · preset {item.profile.encoder_preset} · resolution-based CQ{item.built_in ? ' · built in' : ' · custom'}</span></span>
                                </label>
                                <div className="mt-2 flex flex-wrap gap-1 text-[10px] font-semibold uppercase">
                                    {(['480p', '720p', '1080p', '2160p'] as const).filter((scope) => config[`default_handbrake_profile_${scope}` as keyof Config] === item.profile_id).map((scope) => <span key={scope} className="rounded-full bg-blue-500/20 px-2 py-1 text-blue-200">{scope === '2160p' ? '4K' : scope} default</span>)}
                                </div>
                                <select aria-label={`Assign ${item.display_name} as a resolution default`} defaultValue="" onChange={(event) => { if (event.target.value) void chooseDefaultProfile(item, event.target.value); event.target.value = ''; }} className="mt-3 w-full rounded-lg border border-[var(--border-color)] bg-[var(--bg-tertiary)] px-2 py-2 text-xs text-white">
                                    <option value="">Assign as resolution default…</option><option value="general">General fallback</option><option value="480p">480p sources</option><option value="720p">720p sources</option><option value="1080p">1080p sources</option><option value="2160p">4K sources</option>
                                </select>
                                <div className="flex gap-2 mt-3"><button type="button" className="btn btn-secondary text-xs" onClick={() => editProfile(item, item.built_in)}>{item.built_in ? 'Customize' : 'Edit'}</button><button type="button" className="btn btn-secondary text-xs" onClick={() => editProfile(item, true)}>Duplicate</button></div>
                            </div>;
                        })}
                    </div>
                    <div className="text-xs text-[var(--text-muted)]">Use Customize or Duplicate to load any profile into the editor. Built-in profiles remain protected.</div>
                    <div className="rounded-xl border border-[var(--border-color)] p-4 space-y-3">
                        <div className="font-semibold text-white">Create or replace a custom profile</div>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                            <label className="space-y-1"><span className="text-xs font-semibold text-white">Profile ID <span className="text-red-300">(required)</span></span><input id="handbrake-profile-id" value={profileDraft.profile_id} onChange={(event) => setProfileDraft({ ...profileDraft, profile_id: event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, '-') })} placeholder="my-profile" className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white" /><span className="block text-xs text-[var(--text-muted)]">For example: amd-stereo-surround</span></label>
                            <label className="space-y-1"><span className="text-xs font-semibold text-white">Display name <span className="text-red-300">(required)</span></span><input id="handbrake-profile-display-name" value={profileDraft.display_name} onChange={(event) => setProfileDraft({ ...profileDraft, display_name: event.target.value })} placeholder="AMD Stereo + 5.1" className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white" /><span className="block text-xs text-[var(--text-muted)]">The friendly name shown on discs and queue reviews.</span></label>
                            <select value={profileDraft.encoder} onChange={(event) => {
                                const encoder = event.target.value;
                                const encoder_preset = encoder === 'x264' || encoder === 'x265' ? 'medium' : encoder === 'svt_av1' ? '8' : encoder.startsWith('nvenc_') ? 'medium' : 'balanced';
                                setProfileDraft({ ...profileDraft, encoder, encoder_preset });
                            }} className="bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white">
                                <option value="vce_h264">AMD VCN H.264</option><option value="vce_h265">AMD VCN H.265</option><option value="nvenc_h264">NVIDIA NVENC H.264</option><option value="nvenc_h265">NVIDIA NVENC H.265</option><option value="qsv_h264">Intel Quick Sync H.264</option><option value="qsv_h265">Intel Quick Sync H.265</option><option value="x264">CPU x264</option><option value="x265">CPU x265</option><option value="svt_av1">CPU SVT-AV1</option>
                            </select>
                            <label className="space-y-1"><span className="text-xs text-[var(--text-muted)]">Encoder preset</span><select value={profileDraft.encoder_preset} onChange={(event) => setProfileDraft({ ...profileDraft, encoder_preset: event.target.value })} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white">
                                {(profileDraft.encoder === 'x264' || profileDraft.encoder === 'x265' ? ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'] : profileDraft.encoder === 'svt_av1' ? Array.from({ length: 14 }, (_, index) => String(index)) : profileDraft.encoder.startsWith('nvenc_') ? ['fast', 'medium', 'slow'] : ['speed', 'balanced', 'quality']).map((preset) => <option key={preset} value={preset}>{preset}</option>)}
                            </select></label>
                            <div className="col-span-full text-xs text-[var(--text-muted)]">Quality is selected automatically from the four resolution-specific values below based on the detected source height.</div>
                            <label className="space-y-1"><span className="text-xs text-[var(--text-muted)]">Content type</span><select value={profileDraft.content_kind} onChange={(event) => setProfileDraft({ ...profileDraft, content_kind: event.target.value, nlmeans_preset: event.target.value === 'live_action' ? profileDraft.nlmeans_preset : '', nlmeans_tune: event.target.value === 'live_action' ? profileDraft.nlmeans_tune : 'none' })} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white"><option value="unknown">Unknown / general</option><option value="live_action">Live action</option><option value="animation">Animation</option></select></label>
                            <label className="space-y-1"><span className="text-xs text-[var(--text-muted)]">NLMeans denoise</span><select disabled={profileDraft.content_kind !== 'live_action'} value={profileDraft.nlmeans_preset} onChange={(event) => setProfileDraft({ ...profileDraft, nlmeans_preset: event.target.value, nlmeans_tune: event.target.value ? 'film' : 'none' })} className="w-full disabled:opacity-50 bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white"><option value="">Off</option><option value="ultralight">Ultralight</option><option value="light">Light</option><option value="medium">Medium</option><option value="strong">Strong</option></select></label>
                            <label className="space-y-1"><span className="text-xs text-[var(--text-muted)]">NLMeans tune</span><select disabled={!profileDraft.nlmeans_preset} value={profileDraft.nlmeans_tune} onChange={(event) => setProfileDraft({ ...profileDraft, nlmeans_tune: event.target.value })} className="w-full disabled:opacity-50 bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white">{['none', 'film', 'grain', 'highmotion', 'animation', 'tape', 'sprite'].map((tune) => <option key={tune} value={tune}>{tune}</option>)}</select></label>
                            <fieldset className="col-span-full rounded-lg border border-[var(--border-color)] p-3 space-y-3">
                                <legend className="px-1 text-xs font-semibold text-white">Audio output order and layouts</legend>
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                                    <label className="space-y-1"><span className="text-xs text-[var(--text-muted)]">Primary/default audio track</span><select value={profileDraft.audio_primary_layout} onChange={(event) => setProfileDraft({ ...profileDraft, audio_primary_layout: event.target.value })} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white"><option value="default">Disc-default passthrough</option><option value="highest">Highest available passthrough</option><option value="stereo">Stereo (2.0)</option><option value="2.1">2.1</option><option value="5.1">5.1 surround</option><option value="7.1">7.1 surround</option></select></label>
                                    <label className="space-y-1"><span className="text-xs text-[var(--text-muted)]">Secondary audio track</span><select value={profileDraft.audio_secondary_layout} onChange={(event) => setProfileDraft({ ...profileDraft, audio_secondary_layout: event.target.value })} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white"><option value="none">No secondary track</option><option value="default">Disc-default passthrough</option><option value="highest">Highest available passthrough</option><option value="stereo">Stereo (2.0)</option><option value="2.1">2.1</option><option value="5.1">5.1 surround</option><option value="7.1">7.1 surround</option></select></label>
                                </div>
                                {profileDraft.audio_primary_layout === profileDraft.audio_secondary_layout && profileDraft.audio_secondary_layout !== 'none' && <div className="rounded-lg border border-amber-400/40 bg-amber-500/10 p-2 text-xs text-amber-100">Primary and secondary layouts must be different.</div>}
                                <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                                    {[
                                        ['Stereo', 'audio_bitrate_stereo'],
                                        ['2.1', 'audio_bitrate_2_1'],
                                        ['5.1', 'audio_bitrate_5_1'],
                                        ['7.1', 'audio_bitrate_7_1'],
                                    ].map(([label, bitrateField]) => <label key={bitrateField} className="rounded-lg border border-[var(--border-color)] p-2 text-xs text-[var(--text-muted)]"><span className="block mb-1">{label} bitrate</span><span className="flex items-center gap-1"><input type="number" min="64" max="1024" step="32" value={Number(profileDraft[bitrateField as keyof typeof profileDraft])} onChange={(event) => setProfileDraft({ ...profileDraft, [bitrateField]: Number(event.target.value) })} className="w-full rounded border border-[var(--border-color)] bg-[var(--bg-primary)] px-2 py-1 text-right text-white" />kbps</span></label>)}
                                </div>
                                <span className="block text-xs text-[var(--text-muted)]">Track order is explicit: the primary track is first and marked as the default; the secondary track follows. Bitrates apply to encoded layouts. Passthrough choices retain the source encoding.</span>
                            </fieldset>
                            <fieldset className="col-span-full rounded-lg border border-[var(--border-color)] p-3 text-sm space-y-2">
                                <legend className="px-1 text-xs font-semibold text-white">Language and additional-track retention</legend>
                                <label className="flex items-start gap-2"><input type="radio" name="audio-retention" className="mt-1" disabled={profileDraft.audio_language.split(',').filter(Boolean).length > 1} checked={profileDraft.additional_audio !== 'all'} onChange={() => { const languages = profileDraft.audio_language === 'default' ? ['eng'] : profileDraft.audio_language.split(',').filter(Boolean); const selected = languages[0] || 'eng'; setAudioModeWarning(languages.length > 1 ? `Preferred-track-only accepts one language. Only ${selected} will be retained; ${languages.slice(1).join(', ')} will be removed from this mode.` : null); setProfileDraft({ ...profileDraft, audio_language: selected, additional_audio: 'selected_only' }); setAudioChoicePrompt(false); }} /><span><span className="block text-xs font-semibold text-white">Keep preferred track only</span><span className="block text-xs text-[var(--text-muted)]">Keeps one selected layout/language track. Disabled when multiple languages are selected.</span></span></label>
                                <label className="flex items-start gap-2"><input type="radio" name="audio-retention" className="mt-1" checked={profileDraft.additional_audio === 'all' && profileDraft.audio_language !== 'default'} onChange={() => { const languages = profileDraft.audio_language === 'default' ? 'eng' : profileDraft.audio_language; setAudioPromptLanguage(languages); setAudioModeWarning(null); setProfileDraft({ ...profileDraft, audio_language: languages, additional_audio: 'all' }); setAudioChoicePrompt(true); }} /><span><span className="block text-xs font-semibold text-white">Keep all specified languages</span><span className="block text-xs text-[var(--text-muted)]">Keeps every matching track, such as eng,spa.</span></span></label>
                                <label className="flex items-start gap-2"><input type="radio" name="audio-retention" className="mt-1" checked={profileDraft.additional_audio === 'all' && profileDraft.audio_language === 'default'} onChange={() => { setAudioModeWarning(null); setProfileDraft({ ...profileDraft, additional_audio: 'all', audio_language: 'default' }); setAudioChoicePrompt(false); }} /><span><span className="block text-xs font-semibold text-white">Keep all source audio tracks</span><span className="block text-xs text-[var(--text-muted)]">Keeps every source language, dub, commentary, and alternate track.</span></span></label>
                                {audioModeWarning && <div className="rounded-lg border border-amber-400/40 bg-amber-500/10 p-2 text-xs text-amber-100">{audioModeWarning}</div>}
                                <div className="text-xs text-[var(--text-muted)]">{profileDraft.additional_audio === 'all' ? `All ${profileDraft.audio_language === 'default' ? 'source' : `matching ${profileDraft.audio_language}`} audio tracks will be retained.` : `Only the preferred ${profileDraft.audio_language === 'default' ? 'source-default' : profileDraft.audio_language} track will be retained.`}</div>
                                {profileDraft.audio_language !== 'default' && <div className="rounded-lg border border-[var(--border-color)] bg-[var(--bg-tertiary)]/50 p-2 text-xs text-[var(--text-muted)]">Retained audio languages: <span className="font-semibold text-white">{profileDraft.audio_language}</span></div>}
                                {profileDraft.audio_language !== 'default' && profileDraft.audio_default_language === 'default' && <div className="rounded-lg border border-amber-400/40 bg-amber-500/10 p-3 space-y-2"><div className="text-xs font-semibold text-amber-100">Choose the preferred default audio language</div><select value="default" onChange={(event) => setProfileDraft({ ...profileDraft, audio_default_language: event.target.value })} className="w-full bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white"><option value="default">Select a default language</option>{profileDraft.audio_language.split(',').filter(Boolean).map((code) => <option key={code} value={code}>{code}</option>)}</select></div>}
                            </fieldset>
                            {audioChoicePrompt && <div className="col-span-full rounded-xl border border-indigo-400/40 bg-indigo-500/10 p-4 space-y-3">
                                <div className="font-semibold text-white">Preferred audio language</div>
                                <p className="text-sm text-[var(--text-muted)]">Enter one or more preferred languages. HandBrake will retain all matching tracks when you choose the all-matching option.</p>
                                <select multiple size={8} value={audioPromptLanguage.split(',').filter(Boolean)} onChange={(event) => { const languages = Array.from(event.target.selectedOptions, (option) => option.value).join(','); setAudioPromptLanguage(languages); setProfileDraft({ ...profileDraft, audio_language: languages, additional_audio: 'all' }); setLanguageMultiSelectHint(true); }} className="w-full bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white">
                                    {AUDIO_LANGUAGE_OPTIONS.filter(([code]) => code !== 'default' && code !== 'und').map(([code, name]) => <option key={code} value={code}>{name} ({code})</option>)}
                                </select>
                                {languageMultiSelectHint && <div className="rounded-lg border border-indigo-400/40 bg-indigo-500/10 p-2 text-xs text-indigo-100">Tip: hold Ctrl while clicking to select multiple languages. On macOS, hold Command.</div>}
                                <div className="text-xs text-[var(--text-muted)]">Selected: {audioPromptLanguage || 'none'}</div>
                                <div className="flex flex-wrap gap-2">
                                    <button type="button" className="btn btn-primary" onClick={() => { setProfileDraft({ ...profileDraft, audio_language: audioPromptLanguage, additional_audio: 'selected_only' }); setAudioChoicePrompt(false); }}>Keep only the preferred track</button>
                                    <button type="button" className="btn btn-secondary" onClick={() => { setProfileDraft({ ...profileDraft, audio_language: audioPromptLanguage, additional_audio: 'all' }); setAudioChoicePrompt(false); }}>Keep all matching language tracks</button>
                                    <button type="button" className="btn btn-secondary" onClick={() => { setProfileDraft({ ...profileDraft, additional_audio: 'selected_only' }); setAudioChoicePrompt(false); }}>Cancel</button>
                                </div>
                            </div>}
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
                            <label className="space-y-1"><span className="text-xs text-[var(--text-muted)]">Preferred CQ 480p</span><input type="number" min="0" max="51" step="0.5" value={profileDraft.quality_480p} onChange={(event) => setProfileDraft({ ...profileDraft, quality_480p: Number(event.target.value) })} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white" /></label>
                            <label className="space-y-1"><span className="text-xs text-[var(--text-muted)]">Preferred CQ 720p</span><input type="number" min="0" max="51" step="0.5" value={profileDraft.quality_720p} onChange={(event) => setProfileDraft({ ...profileDraft, quality_720p: Number(event.target.value) })} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white" /></label>
                            <label className="space-y-1"><span className="text-xs text-[var(--text-muted)]">Preferred CQ 1080p</span><input type="number" min="0" max="51" step="0.5" value={profileDraft.quality_1080p} onChange={(event) => setProfileDraft({ ...profileDraft, quality_1080p: Number(event.target.value) })} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white" /></label>
                            <label className="space-y-1"><span className="text-xs text-[var(--text-muted)]">Preferred CQ 4K</span><input type="number" min="0" max="51" step="0.5" value={profileDraft.quality_2160p} onChange={(event) => setProfileDraft({ ...profileDraft, quality_2160p: Number(event.target.value) })} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white" /></label>
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
                            <label className="space-y-1 rounded-lg border border-[var(--border-color)] p-3"><span className="block text-xs text-[var(--text-muted)]">Maximum resolution</span><select value={profileDraft.resolution_policy} onChange={(event) => setProfileDraft({ ...profileDraft, resolution_policy: event.target.value })} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white"><option value="source">Same as source</option><option value="480p">Up to 480p</option><option value="720p">Up to 720p</option><option value="1080p">Up to 1080p</option><option value="2160p">Up to 2160p</option></select></label>
                            <label className="space-y-1 rounded-lg border border-[var(--border-color)] p-3"><span className="block text-xs text-[var(--text-muted)]">Frame-rate policy</span><select value={profileDraft.frame_rate_policy} onChange={(event) => setProfileDraft({ ...profileDraft, frame_rate_policy: event.target.value })} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white"><option value="source">Same as source timing</option><option value="vfr">Force VFR</option>{['23.976', '24', '25', '29.97', '30', '50', '59.94', '60'].map((rate) => <option key={rate} value={rate}>{rate} fps (PFR)</option>)}</select></label>
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
                            <label className="space-y-1 rounded-lg border border-[var(--border-color)] p-3"><span className="block text-xs text-[var(--text-muted)]">Preferred/default audio language</span><select value={AUDIO_LANGUAGE_OPTIONS.some(([code]) => code === profileDraft.audio_default_language) ? profileDraft.audio_default_language : 'custom'} onChange={(event) => setProfileDraft({ ...profileDraft, audio_default_language: event.target.value === 'custom' ? '' : event.target.value })} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white">{AUDIO_LANGUAGE_OPTIONS.map(([code, name]) => <option key={code} value={code}>{name} ({code})</option>)}<option value="custom">Other ISO-639-2 code</option></select>{(!AUDIO_LANGUAGE_OPTIONS.some(([code]) => code === profileDraft.audio_default_language) || profileDraft.audio_default_language === '') && <input value={profileDraft.audio_default_language} onChange={(event) => setProfileDraft({ ...profileDraft, audio_default_language: event.target.value.toLowerCase() })} placeholder="eng" className="w-full mt-2 bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white" />}<span className="text-xs text-[var(--text-muted)]">This one language is marked as the default audio track.</span></label>
                            <label className="space-y-1 rounded-lg border border-[var(--border-color)] p-3"><span className="block text-xs text-[var(--text-muted)]">Audio language selection</span><select value={profileDraft.audio_selection} onChange={(event) => setProfileDraft({ ...profileDraft, audio_selection: event.target.value })} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white"><option value="all_matching">All matching languages</option><option value="first_matching">First matching language</option><option value="all">All languages</option></select><span className="block text-xs text-[var(--text-muted)]">{profileDraft.audio_selection === 'all' ? 'All source-language tracks' : profileDraft.audio_selection === 'first_matching' ? 'First matching track' : `All tracks matching: ${profileDraft.audio_language === 'default' ? 'source default' : profileDraft.audio_language}`}</span></label>
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
                            <label className="rounded-lg border border-[var(--border-color)] p-3"><input type="checkbox" className="mr-2" checked={profileDraft.selective_decomb} onChange={(event) => setProfileDraft({ ...profileDraft, selective_decomb: event.target.checked })} />Detect combing and decomb</label>
                            <label className="space-y-1 rounded-lg border border-[var(--border-color)] p-3"><span className="block text-xs text-[var(--text-muted)]">Subtitle languages</span><select multiple size={6} value={profileDraft.subtitle_language.split(',').filter(Boolean)} onChange={(event) => { setProfileDraft({ ...profileDraft, subtitle_language: Array.from(event.target.selectedOptions, (option) => option.value).join(',') }); setSubtitleLanguageHint(true); }} className="w-full bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white">{AUDIO_LANGUAGE_OPTIONS.filter(([code]) => code !== 'default' && code !== 'und').map(([code, name]) => <option key={code} value={code}>{name} ({code})</option>)}</select><span className="text-xs text-[var(--text-muted)]">Selected: {profileDraft.subtitle_language || 'none'}</span>{subtitleLanguageHint && <span className="block rounded-lg border border-indigo-400/40 bg-indigo-500/10 p-2 text-xs text-indigo-100">Hold Ctrl while clicking to select multiple subtitle languages.</span>}</label>
                            <label className="space-y-1 rounded-lg border border-[var(--border-color)] p-3"><span className="block text-xs text-[var(--text-muted)]">Subtitle selection</span><select value={profileDraft.subtitle_selection} onChange={(event) => setProfileDraft({ ...profileDraft, subtitle_selection: event.target.value, retain_subtitles: event.target.value !== 'none' })} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white"><option value="all_matching">All selected-language tracks</option><option value="first_matching">First selected-language track</option><option value="all">All languages</option><option value="none">No subtitle tracks</option></select></label>
                            <label className="space-y-1 rounded-lg border border-[var(--border-color)] p-3"><span className="block text-xs text-[var(--text-muted)]">Default subtitle</span><select value={profileDraft.subtitle_default} onChange={(event) => setProfileDraft({ ...profileDraft, subtitle_default: event.target.value })} className="w-full bg-[var(--bg-tertiary)] border border-[var(--border-color)] rounded-lg px-3 py-2 text-white"><option value="none">No automatic subtitle</option><option value="first">First selected subtitle</option></select></label>
                        </div>
                        <div className="rounded-lg border border-blue-400/30 bg-blue-500/10 p-3 text-sm text-blue-100">
                            <div className="font-semibold">Output audio plan</div>
                            <div className="mt-1">Track 1 (default): {profileDraft.audio_primary_layout}</div>
                            <div>{profileDraft.audio_secondary_layout === 'none' ? 'No secondary layout track' : `Track 2: ${profileDraft.audio_secondary_layout}`}</div>
                            <div className="mt-1 text-xs text-blue-100/70">{profileDraft.additional_audio === 'all' ? `Additional retained tracks: ${profileDraft.audio_language === 'default' ? 'all source audio tracks and languages' : `all matching ${profileDraft.audio_language} tracks`}.` : `Additional tracks are not retained; the preferred ${profileDraft.audio_language === 'default' ? 'source-default' : profileDraft.audio_language} track supplies the two-track pair above.`}</div>
                        </div>
                        <div className="flex flex-wrap items-center gap-3">
                            <button type="button" className="btn btn-secondary" disabled={profileDraft.audio_secondary_layout !== 'none' && profileDraft.audio_primary_layout === profileDraft.audio_secondary_layout} onClick={saveProfile}>Save custom profile</button>
                            {profileSaveResult && <div role="status" className={`rounded-lg border px-3 py-2 text-sm ${profileSaveResult.type === 'success' ? 'border-green-500/40 bg-green-500/10 text-green-200' : 'border-red-500/40 bg-red-500/10 text-red-200'}`}>{profileSaveResult.text}</div>}
                        </div>
                    </div>
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
                                            id="settings-opensubtitles-username"
                                            type="text"
                                            value={config.open_subtitles_username || ''}
                                            onChange={(e) => handleChange('open_subtitles_username', e.target.value)}
                                            placeholder={config.credential_status?.['opensubtitles-username']?.configured ? 'Configured — paste only to replace' : 'Not configured'}
                                            className="w-full bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-lg px-4 py-2 text-white focus:outline-none focus:border-[var(--accent-primary)]"
                                        />
                                    </div>
                                    <div className="space-y-2">
                                        <label className="text-sm font-medium text-muted">Password</label>
                                        <input
                                            id="settings-opensubtitles-password"
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
                                            id="settings-opensubtitles-api-key"
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
                                id="settings-tmdb-api-key"
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
                        {saving ? 'Saving configuration…' : message?.type === 'success' && message.text === 'Settings saved successfully' ? '✓ Configuration saved' : 'Save Configuration'}
                    </button>
                </div>
            </form>
        </div>
    );
};

export default SettingsView;
