import { useMemo, useState } from 'react';

interface SupportViewProps {
  version: string;
  onOpenSettings: () => void;
}

interface BundleInfo {
  file: File;
  filename: string;
  supportId: string;
  sha256: string;
}

const BUG_REPORT_URL = 'https://github.com/fajis1/ripweaver/issues/new';

const responseFilename = (header: string | null) => {
  const match = header?.match(/filename="?([^";]+)"?/i);
  return match?.[1] || 'RipWeaver-Support.zip';
};

const downloadFile = (file: File) => {
  const url = URL.createObjectURL(file);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = file.name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
};

const SupportView = ({ version, onOpenSettings }: SupportViewProps) => {
  const [description, setDescription] = useState('');
  const [working, setWorking] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [bundle, setBundle] = useState<BundleInfo | null>(null);

  const reportBody = useMemo(() => {
    const problem = description.trim() || '[Describe what happened and what you expected.]';
    return [
      '## What happened',
      problem,
      '',
      '## RipWeaver version',
      version,
      '',
      '## Support bundle',
      bundle
        ? `Support ID: ${bundle.supportId}\nBundle: ${bundle.filename}\nSHA-256: ${bundle.sha256}`
        : 'Create a support bundle in RipWeaver, then attach the downloaded ZIP.',
      '',
      'Please drag the ZIP into this report. Do not attach media, .env files, or raw private provider evidence.',
    ].join('\n');
  }, [bundle, description, version]);

  const createBundle = async () => {
    if (working) return;
    setWorking(true);
    setError('');
    setNotice('');
    try {
      const response = await fetch('/system/support-bundle', { method: 'POST' });
      if (!response.ok) {
        let message = 'RipWeaver could not create the support bundle safely.';
        try {
          const payload = await response.json();
          if (typeof payload.detail === 'string') message = payload.detail;
        } catch {
          // The generic local error is intentionally enough when no JSON is returned.
        }
        throw new Error(message);
      }
      const blob = await response.blob();
      const filename = responseFilename(response.headers.get('Content-Disposition'));
      const file = new File([blob], filename, { type: 'application/zip' });
      const result = {
        file,
        filename,
        supportId: response.headers.get('X-RipWeaver-Support-ID') || 'not-provided',
        sha256: response.headers.get('X-RipWeaver-Support-SHA256') || 'not-provided',
      };
      setBundle(result);
      downloadFile(file);
      setNotice('Support ZIP created and downloaded. Review it, then attach it to your report.');
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'RipWeaver could not create the support bundle safely.');
    } finally {
      setWorking(false);
    }
  };

  const shareBundle = async () => {
    if (
      !bundle
      || typeof navigator.share !== 'function'
      || !navigator.canShare?.({ files: [bundle.file] })
    ) return;
    try {
      await navigator.share({
        title: `RipWeaver ${version} bug report`,
        text: `RipWeaver support bundle ${bundle.supportId}`,
        files: [bundle.file],
      });
    } catch (shareError) {
      if (shareError instanceof DOMException && shareError.name === 'AbortError') return;
      setError('The operating-system share window could not be opened. The ZIP is still in Downloads.');
    }
  };

  const openGitHubReport = () => {
    const url = new URL(BUG_REPORT_URL);
    url.searchParams.set('title', '[Bug] ');
    url.searchParams.set('body', reportBody);
    window.open(url.toString(), '_blank', 'noopener,noreferrer');
  };

  const openEmailDraft = () => {
    const subject = encodeURIComponent(`RipWeaver ${version} bug report`);
    const body = encodeURIComponent(`${reportBody}\n\nAttach the downloaded ZIP to this message before sending.`);
    window.location.href = `mailto:?subject=${subject}&body=${body}`;
  };

  const canShareBundle = Boolean(
    bundle
    && typeof navigator.share === 'function'
    && navigator.canShare?.({ files: [bundle.file] }),
  );

  return (
    <div className="mx-auto max-w-4xl space-y-6 animate-fade-in">
      <div>
        <h2 className="text-3xl font-bold heading-gradient">Support &amp; Bug Reports</h2>
        <p className="mt-2 text-sm text-[var(--text-muted)]">
          Create one privacy-redacted ZIP that helps diagnose a problem. RipWeaver creates it locally and never uploads or emails it automatically.
        </p>
      </div>

      {error && <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-red-200">{error}</div>}
      {notice && <div className="rounded-xl border border-green-500/30 bg-green-500/10 p-4 text-green-200">{notice}</div>}

      <section className="glass-panel space-y-5 rounded-2xl p-6">
        <div>
          <h3 className="text-xl font-bold text-white">1. Create the support ZIP</h3>
          <p className="mt-2 text-sm text-[var(--text-muted)]">
            The export includes setup status, recent pipeline events, and bounded tails of RipWeaver application logs. Paths, media names, credentials, dialogue, environment values, and private provider responses are excluded or redacted.
          </p>
        </div>
        <div className="rounded-xl border border-amber-400/25 bg-amber-400/10 p-4 text-sm text-amber-100">
          Automated redaction is deliberately conservative, but you should still review the ZIP before sharing it. Never add media files, an <code>.env</code> file, or raw transcript/provider evidence.
        </div>
        <div className="flex flex-wrap gap-3">
          <button type="button" className="btn btn-primary" disabled={working} onClick={createBundle}>
            {working ? 'Creating support ZIP…' : 'Create & download support ZIP'}
          </button>
          {bundle && (
            <button type="button" className="btn btn-secondary" onClick={() => downloadFile(bundle.file)}>
              Download ZIP again
            </button>
          )}
          {canShareBundle && (
            <button type="button" className="btn btn-secondary" onClick={shareBundle}>
              Share ZIP…
            </button>
          )}
        </div>
        {bundle && (
          <div className="rounded-xl border border-[var(--border-color)] bg-[var(--bg-tertiary)]/40 p-4 text-sm">
            <div className="font-semibold text-white">{bundle.filename}</div>
            <div className="mt-2 font-mono text-xs text-[var(--text-muted)]">Support ID: {bundle.supportId}</div>
            <div className="mt-1 break-all font-mono text-xs text-[var(--text-muted)]">SHA-256: {bundle.sha256}</div>
          </div>
        )}
      </section>

      <section className="glass-panel space-y-5 rounded-2xl p-6">
        <div>
          <h3 className="text-xl font-bold text-white">2. Describe and send the report</h3>
          <p className="mt-2 text-sm text-[var(--text-muted)]">
            Include what you were doing, what you expected, what happened instead, and roughly when it occurred.
          </p>
        </div>
        <textarea
          className="input-field min-h-36 w-full resize-y"
          value={description}
          maxLength={2_000}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="Example: I inserted a TV disc and clicked Prepare, but RipWeaver reported…"
          aria-label="Bug description"
        />
        <div className="flex flex-wrap gap-3">
          <button type="button" className="btn btn-primary" onClick={openGitHubReport}>
            Open GitHub bug report
          </button>
          <button type="button" className="btn btn-secondary" onClick={openEmailDraft}>
            Open email draft
          </button>
          <button type="button" className="btn btn-secondary" onClick={onOpenSettings}>
            Check keys and settings
          </button>
        </div>
        <p className="text-xs text-[var(--text-muted)]">
          Browsers cannot attach a file to an email or GitHub issue without your approval. After the draft opens, attach or drag in the downloaded ZIP before sending.
        </p>
      </section>
    </div>
  );
};

export default SupportView;
