# coding=utf-8
import re

with open('mkv_episode_matcher/frontend/src/components/RipPipelineView.tsx', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace('  match_summary: string | null;', '  match_summary: string | null;\n  user_hint: string | null;\n  assessed_role: string | null;\n  current_route: string | null;\n  evidence_status: string | null;')

# I will replace ID: {item.media_id} without the unicode character by using a regex.
pattern = r'(<div className="text-xs text-slate-400">\s*ID: \{item\.media_id\}.*?</div>)'
replacement = r'\1\n                    {(item.user_hint || item.assessed_role || item.evidence_status || item.current_route) && (\n                      <div className="mt-2 text-xs text-indigo-200/80">\n                        {item.user_hint && <div className="text-indigo-200/80">User hint: {item.user_hint}</div>}\n                        <div className="mt-1 flex flex-wrap gap-2 opacity-80">\n                          {item.assessed_role && <span className="rounded bg-indigo-500/20 px-1.5 py-0.5 border border-indigo-500/30">Role: {item.assessed_role}</span>}\n                          {item.current_route && <span className="rounded bg-indigo-500/20 px-1.5 py-0.5 border border-indigo-500/30">Route: {item.current_route}</span>}\n                          {item.evidence_status && <span className="rounded bg-indigo-500/20 px-1.5 py-0.5 border border-indigo-500/30">Evidence: {item.evidence_status}</span>}\n                        </div>\n                      </div>\n                    )}'

text = re.sub(pattern, replacement, text)

with open('mkv_episode_matcher/frontend/src/components/RipPipelineView.tsx', 'w', encoding='utf-8') as f:
    f.write(text)
