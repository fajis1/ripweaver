import re

with open('mkv_episode_matcher/frontend/src/components/RipPipelineView.tsx', 'r', encoding='utf-8') as f:
    text = f.read()

pattern = r'(\{item\.match_summary && <div className="mt-1 max-w-2xl text-xs text-\[var\(--text-muted\)\].*?</div>\})'
replacement = r'''\1
                      {(item.user_hint || item.assessed_role || item.evidence_status || item.current_route) && (
                        <div className="mt-2 text-xs text-indigo-200/80">
                          {item.user_hint && <div className="text-indigo-200/80">User hint: {item.user_hint}</div>}
                          <div className="mt-1 flex flex-wrap gap-2 opacity-80">
                            {item.assessed_role && <span className="rounded bg-indigo-500/20 px-1.5 py-0.5 border border-indigo-500/30">Role: {item.assessed_role}</span>}
                            {item.current_route && <span className="rounded bg-indigo-500/20 px-1.5 py-0.5 border border-indigo-500/30">Route: {item.current_route}</span>}
                            {item.evidence_status && <span className="rounded bg-indigo-500/20 px-1.5 py-0.5 border border-indigo-500/30">Evidence: {item.evidence_status}</span>}
                          </div>
                        </div>
                      )}'''

text = re.sub(pattern, replacement, text)

with open('mkv_episode_matcher/frontend/src/components/RipPipelineView.tsx', 'w', encoding='utf-8') as f:
    f.write(text)
