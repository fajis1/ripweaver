import re

with open('mkv_episode_matcher/frontend/src/components/RipPipelineView.tsx', 'r', encoding='utf-8') as f:
    text = f.read()

# I will add the `reassessDiscMetadata` function next to `deleteQueuedStagedSource`
func_anchor = r'  const deleteQueuedStagedSource = async \(item: PipelineQueueItem\) => \{'
reassess_func = '''  const reassessDiscMetadata = async (group: AttentionDiscGroup) => {
    const fingerprint = group.key.replace('Disc: ', '');
    if (!fingerprint || fingerprint.startsWith('untracked')) return;
    const content_hint = window.prompt(`Run metadata-only reassessment for "${group.label}"?\\nEnter content hint (tv, movie, extras) or leave blank for unknown:`);
    if (content_hint === null) return;
    const trimmedHint = content_hint.trim().toLowerCase();
    const hint = trimmedHint === 'tv' || trimmedHint === 'movie' || trimmedHint === 'extras' ? trimmedHint : null;
    setControlling(true);
    setError('');
    try {
      const response = await fetch(`/rip/pipeline/discs/${encodeURIComponent(fingerprint)}/reassess`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content_hint: hint, confirm_reassessment: true }),
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Metadata reassessment failed.');
      setReviewNotice(`Reassessment complete for ${group.label}. You may now restart identification for individual titles.`);
      await fetchQueue();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Metadata reassessment failed.');
    } finally {
      setControlling(false);
    }
  };

  const deleteQueuedStagedSource = async (item: PipelineQueueItem) => {'''

text = re.sub(func_anchor, reassess_func, text)

# I will add the button to the UI header.
button_anchor = r'\{group\.failedCount > 0 && \(\s*<span className="rounded-full border border-red-300/35 bg-red-400/10 px-2\.5 py-1 text-red-100">\s*\{group\.failedCount\} failed\s*</span>\s*\)\}'

button_html = '''{group.failedCount > 0 && (
                              <span className="rounded-full border border-red-300/35 bg-red-400/10 px-2.5 py-1 text-red-100">
                                {group.failedCount} failed
                              </span>
                            )}
                            {!group.key.startsWith('untracked') && (
                              <button
                                type="button"
                                className="ml-2 btn btn-secondary text-[10px] px-2 py-0.5 rounded border border-blue-400/50 text-blue-100 hover:bg-blue-500/20"
                                disabled={controlling}
                                onClick={(e) => {
                                  e.preventDefault();
                                  e.stopPropagation();
                                  void reassessDiscMetadata(group);
                                }}
                              >
                                Reassess metadata
                              </button>
                            )}'''

text = re.sub(button_anchor, button_html, text)

with open('mkv_episode_matcher/frontend/src/components/RipPipelineView.tsx', 'w', encoding='utf-8') as f:
    f.write(text)
