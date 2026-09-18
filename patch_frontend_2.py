# coding=utf-8
import re

with open('mkv_episode_matcher/frontend/src/components/RipPipelineView.tsx', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace('  user_hint: string | null;\n  assessed_role: string | null;\n  current_route: string | null;\n  evidence_status: string | null;', '  user_hint: string | null;\n  assessed_composition: string | null;\n  assessed_role: string | null;\n  current_route: string | null;\n  evidence_status: string | null;\n  exhausted_reason: string | null;')

pattern = r'\{\(item\.user_hint \|\| item\.assessed_role \|\| item\.evidence_status \|\| item\.current_route\) && \(\s*<div className="mt-2 text-xs text-indigo-200/80">\s*\{item\.user_hint && <div className="text-indigo-200/80">User hint: \{item\.user_hint\}</div>\}\s*<div className="mt-1 flex flex-wrap gap-2 opacity-80">\s*\{item\.assessed_role && <span className="rounded bg-indigo-500/20 px-1\.5 py-0\.5 border border-indigo-500/30">Role: \{item\.assessed_role\}</span>\}\s*\{item\.current_route && <span className="rounded bg-indigo-500/20 px-1\.5 py-0\.5 border border-indigo-500/30">Route: \{item\.current_route\}</span>\}\s*\{item\.evidence_status && <span className="rounded bg-indigo-500/20 px-1\.5 py-0\.5 border border-indigo-500/30">Evidence: \{item\.evidence_status\}</span>\}\s*</div>\s*</div>\s*\)\}'

replacement = r'''{(item.user_hint || item.assessed_composition || item.assessed_role || item.evidence_status || item.current_route || item.exhausted_reason) && (
                        <div className="mt-2 text-xs text-indigo-200/80">
                          {item.user_hint && <div className="text-indigo-200/80">User hint: {item.user_hint}</div>}
                          <div className="mt-1 flex flex-wrap gap-2 opacity-80">
                            {item.assessed_composition && <span className="rounded bg-indigo-500/20 px-1.5 py-0.5 border border-indigo-500/30">Composition: {item.assessed_composition.replaceAll('_', ' ')}</span>}
                            {item.assessed_role && <span className="rounded bg-indigo-500/20 px-1.5 py-0.5 border border-indigo-500/30">Role: {item.assessed_role.replaceAll('_', ' ')}</span>}
                            {item.current_route && <span className="rounded bg-indigo-500/20 px-1.5 py-0.5 border border-indigo-500/30">Route: {item.current_route.replaceAll('_', ' ')}</span>}
                            {item.evidence_status && <span className="rounded bg-indigo-500/20 px-1.5 py-0.5 border border-indigo-500/30">Evidence: {item.evidence_status.replaceAll('_', ' ')}</span>}
                            {item.exhausted_reason && <span className="rounded bg-indigo-500/20 px-1.5 py-0.5 border border-indigo-500/30">{item.exhausted_reason === 'exhausted' ? 'Exhausted' : 'Held'}: {item.exhausted_reason.replaceAll('_', ' ')}</span>}
                          </div>
                        </div>
                      )}'''

text = re.sub(pattern, replacement, text)

with open('mkv_episode_matcher/frontend/src/components/RipPipelineView.tsx', 'w', encoding='utf-8') as f:
    f.write(text)
