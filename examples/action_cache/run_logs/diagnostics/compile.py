"""Compile the books.toscrape.com run, isolated from the Roboform artifacts.

Every path is redirected through env vars, including AUTOMATION_JSON, so this
no longer has to swap the Roboform test_automation.json in and out.
"""

import json
import os
from pathlib import Path

BASE = Path("run_logs/site2")
os.environ["AUTOMATION_JSON"] = str(BASE / "test_automation.json")
os.environ["ACTION_CACHE_PATH"] = str(BASE / "action_cache.jsonl")
os.environ["ACTION_DISPATCH_LOG"] = str(BASE / "dispatched_events.jsonl")
os.environ["RUN_REVIEW_PATH"] = str(BASE / "run_review.json")
os.environ["REPAIR_ATTEMPTS_PATH"] = str(BASE / "repair_attempts.json")

from browser_use.action_cache import (  # noqa: E402
	align_cache,
	emit_cached_automation,
	hook_coverage,
	load_run_review,
	locate_cache,
	slice_cache,
	write_hook_coverage,
)

review = load_run_review(force=True)
print(f"run review ({review['source']}): {len(review['values'])} value(s)")
summary = review["summary"]
if not summary["available"]:
	print("  steps: CHECK DID NOT RUN (no LLM) — this is not a pass")
for i, step in enumerate(review["steps"], 1):
	where = f"trace[{step['evidence']}] {step['evidence_label']!r}" if step["evidence"] is not None else "—"
	print(f"  step {i} [{step['verdict']}] {step['description']} | evidence: {where}")
	for note in step["notes"]:
		print(f"      note: {note}")
	if step["repair_prompt"]:
		print(f"      repair ({step['repair_prompt_source']}): {step['repair_prompt']}")
for conflict in summary["conflicts"]:
	print(f"  CONFLICT: {conflict}")

aligned = align_cache(dst=BASE / "action_cache_aligned.jsonl")
sliced = slice_cache(src=BASE / "action_cache_aligned.jsonl", dst=BASE / "action_cache_sliced.jsonl")
located = locate_cache(src=BASE / "action_cache_sliced.jsonl", dst=BASE / "action_cache_located.jsonl")
print(f"\naligned {len(aligned)} -> sliced {len(sliced)} -> located {len(located)}")
for row in located:
	ident = row.get("identity") or {}
	print(f"  [{row.get('label') or '?'}] {ident.get('by')}={str(ident.get('value'))[:60]!r}")

automation = emit_cached_automation(
	src=BASE / "action_cache_located.jsonl",
	dst=BASE / "test_automation_cached.json",
	coverage_dst=BASE / "coverage.json",
)
print(f"\ncached automation: {len(automation['nodes'])} node(s)")
print(f"input_parameters: {automation['parameters']['input_parameters']}")

coverage = json.loads((BASE / "coverage.json").read_text())
print(f"\ncoverage values: required={coverage['required']} cached={coverage['cached']} missing={coverage['missing']}")
print(f"coverage steps: {coverage['step_summary']}")

hooks = hook_coverage(dispatch_path=BASE / "dispatched_events.jsonl", cache_src=BASE / "action_cache.jsonl")
write_hook_coverage(hooks, BASE / "hook_coverage.json")
print(f"\nhook coverage: audit_available={hooks['audit_available']}")
for row in hooks["used_by_workflow"]:
	print(f"  {row['event']}: dispatched {row['dispatched']}, recorded {row['recorded']} [{'ok' if row['hooked'] else 'BLIND SPOT'}]")
