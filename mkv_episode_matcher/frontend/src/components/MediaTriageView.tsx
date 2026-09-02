import React, { useState, useEffect } from 'react';

interface TriageItem {
  filename: string;
  absolute_path: string;
  size_bytes: number;
  recommended_action: string;
  reason: string;
}

interface TriageStatusResponse {
  configured: boolean;
  folder_path: string | null;
}

interface TriageScanResponse {
  items: TriageItem[];
}

interface MediaTriageProps {
  onNavigateToQueue: () => void;
}

const MediaTriageView: React.FC<MediaTriageProps> = ({ onNavigateToQueue }) => {
  const [status, setStatus] = useState<TriageStatusResponse | null>(null);
  const [items, setItems] = useState<TriageItem[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [queueing, setQueueing] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    checkStatus();
  }, []);

  const checkStatus = async () => {
    try {
      const response = await fetch('/triage/status');
      const data: TriageStatusResponse = await response.json();
      setStatus(data);
    } catch (e: any) {
      setError("Failed to fetch triage configuration status.");
    }
  };

  const handleScan = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch('/triage/scan');
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const data: TriageScanResponse = await response.json();
      setItems(data.items);
      setSelected(new Set());
    } catch (e: any) {
      setError(e.message || "Failed to scan folder.");
    } finally {
      setLoading(false);
    }
  };

    const handleQueue = async () => {
    if (selected.size === 0) return;
    setQueueing(true);
    setError(null);
    try {
      const selectedItems = items.filter(i => selected.has(i.absolute_path));
      const response = await fetch('/triage/queue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: selectedItems })
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const data = await response.json();
      // Remove queued items from the list
      setItems(items.filter(i => !selected.has(i.absolute_path)));
      setSelected(new Set());
      // Alert handled by UI notification now
      alert("Successfully queued " + data.queued_count + " items! Click OK, then use the \"View Pipeline Status\" button to track them.");
    } catch (e: any) {
      setError(e.message || "Failed to queue items.");
    } finally {
      setQueueing(false);
    }
  };

  const formatSize = (bytes: number) => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  };

  const getActionBadgeClass = (action: string) => {
    const base = "px-3 py-1 rounded-full text-xs font-bold uppercase tracking-wide border";
    switch (action) {
      case 'organize': return base + " bg-green-500/20 text-green-300 border-green-500/30";
      case 'transcode': return base + " bg-amber-500/20 text-amber-300 border-amber-500/30";
      case 'identify': return base + " bg-blue-500/20 text-blue-300 border-blue-500/30";
      case 'exclude': return base + " bg-slate-500/20 text-slate-400 border-slate-500/30";
        case 'in_pipeline': return base + " bg-purple-500/20 text-purple-300 border-purple-500/30";
      default: return base + " bg-gray-500/20 text-gray-300 border-gray-500/30";
    }
  };

  if (!status) return <div>Loading...</div>;

  return (
    <>
      <div className="max-w-6xl mx-auto glass-panel p-8 rounded-2xl animate-fade-in h-full overflow-y-auto">
      <h2 className="text-3xl font-bold mb-8 heading-gradient">Media Triage</h2>
      <p className="text-[var(--text-muted)] mb-8">Analyze loose files in your staging folder to route them into the RipWeaver pipeline.</p>
      
      {!status.configured ? (
        <div className="p-4 rounded-xl border border-red-500/40 bg-red-500/10 text-red-200">
          <strong>Configuration Required:</strong> No Media Triage Folder is configured in your Settings (.env).
        </div>
      ) : (
        <div className="mb-6 p-5 rounded-xl border border-[var(--border-color)] bg-[var(--bg-tertiary)] shadow-lg">
          <strong>Triage Folder:</strong> {status.folder_path}
            <div className="mt-4">
              <button 
                onClick={handleScan}
                disabled={loading}
                className="btn btn-primary"
              >
                {loading ? "Scanning..." : "Scan Folder"}
              </button>
            </div>
          
        </div>
      )}

      {error && (
        <div className="mb-6 p-4 rounded-xl border border-red-500/40 bg-red-500/10 text-red-200">
          {error}
        </div>
      )}

      {items.length > 0 && (
        <div>
          <h3 className="text-xl font-semibold text-white border-b border-[var(--border-color)] pb-2 mb-4">Found {items.length} Files</h3>
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="bg-[var(--bg-secondary)] text-[var(--text-muted)]">
                <th className="p-3 font-semibold border-b border-[var(--border-color)] w-12">
                    <input 
                      type="checkbox" 
                      onChange={(e) => {
                        if (e.target.checked) {
                          setSelected(new Set(items.filter(i => i.recommended_action !== "in_pipeline" && i.recommended_action !== "exclude").map(i => i.absolute_path)));
                        } else {
                          setSelected(new Set());
                        }
                      }}
                      checked={items.length > 0 && selected.size === items.length}
                    />
                  </th>
                  <th className="p-3 font-semibold border-b border-[var(--border-color)]">Filename</th>
                <th className="p-3 font-semibold border-b border-[var(--border-color)]">Size</th>
                <th className="p-3 font-semibold border-b border-[var(--border-color)]">Recommended Action</th>
                <th className="p-3 font-semibold border-b border-[var(--border-color)]">Reason</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, idx) => (
                <tr key={idx} className="border-b border-[var(--border-color)] hover:bg-[var(--bg-tertiary)]/50 transition-colors">
                  <td className="p-3">
                      <input 
                        type="checkbox"
                        disabled={item.recommended_action === "in_pipeline" || item.recommended_action === "exclude"}
                        checked={selected.has(item.absolute_path)}
                        onChange={(e) => {
                          const newSelected = new Set(selected);
                          if (e.target.checked) {
                            newSelected.add(item.absolute_path);
                          } else {
                            newSelected.delete(item.absolute_path);
                          }
                          setSelected(newSelected);
                        }}
                      />
                    </td>
                    <td className="p-3">
                      <input 
                        type="checkbox"
                        disabled={item.recommended_action === "in_pipeline" || item.recommended_action === "exclude"}
                        checked={selected.has(item.absolute_path)}
                        onChange={(e) => {
                          const newSelected = new Set(selected);
                          if (e.target.checked) {
                            newSelected.add(item.absolute_path);
                          } else {
                            newSelected.delete(item.absolute_path);
                          }
                          setSelected(newSelected);
                        }}
                      />
                    </td>
                    <td className="p-3 text-white">{item.filename}</td>
                  <td className="p-3 text-[var(--text-muted)] whitespace-nowrap">{formatSize(item.size_bytes)}</td>
                  <td className="p-3 text-white">
                    <span className={getActionBadgeClass(item.recommended_action)}>
                      {item.recommended_action.toUpperCase()}
                    </span>
                  </td>
                  <td className="p-3 text-xs text-[var(--text-muted)]">{item.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
          
          
        </div>
      )}
      </div>

      {/* Floating Action Buttons */}
            <div className="absolute bottom-8 right-8 z-[100] flex flex-col items-end gap-4">
        {true && (
          <button 
            onClick={onNavigateToQueue}
            className="btn bg-purple-600 text-white px-8 py-3 text-lg font-bold shadow-xl shadow-purple-500/50 flex items-center gap-3 rounded-full border border-purple-400/30 hover:-translate-y-1 hover:bg-purple-500 transition-all"
          >
            View Pipeline Status
          </button>
        )}
        {items.length > 0 && (
          <button 
            onClick={handleQueue}
            disabled={queueing || selected.size === 0}
            className={`btn bg-green-600 text-white px-8 py-4 text-lg font-bold shadow-2xl shadow-green-500/50 flex items-center gap-3 rounded-full border border-green-400/30 transition-all ${selected.size === 0 || queueing ? 'opacity-50 cursor-not-allowed' : 'hover:-translate-y-1 hover:bg-green-500'}`}
          >
            {queueing ? "Queueing..." : `Queue ${selected.size} Selected`}
          </button>
        )}
      </div>
    </>
  );
};

export default MediaTriageView;
