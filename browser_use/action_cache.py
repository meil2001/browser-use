"""Log executed browser-use actions with live DOM identity.

Stage 1: write JSONL at execute-time (not by parsing terminal logs).
Stage 2: add a stable identity key (name > id > placeholder > test hooks > xpath). Index is debug-only.
Later stages: filter, rank locators, emit Optexity JSON.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from browser_use.dom.views import EnhancedDOMTreeNode, NodeType

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path("run_logs/action_cache.jsonl")
_ALIGNED_PATH = Path("run_logs/action_cache_aligned.jsonl")
_SLICED_PATH = Path("run_logs/action_cache_sliced.jsonl")
_LOCATED_PATH = Path("run_logs/action_cache_located.jsonl")
_CACHED_AUTOMATION_PATH = Path("test_automation_cached.json")
_AUTOMATION_JSON = Path("test_automation.json")
_COVERAGE_PATH = Path("run_logs/coverage.json")
_DISPATCH_LOG_PATH = Path("run_logs/dispatched_events.jsonl")
_HOOK_COVERAGE_PATH = Path("run_logs/hook_coverage.json")
_RUN_REVIEW_PATH = Path("run_logs/run_review.json")
_REPAIR_ATTEMPTS_PATH = Path("run_logs/repair_attempts.json")

# A repair prompt describes intent. Anything that smells like a selector is a
# locator the model invented from a run it only saw a text digest of.
_LOCATOR_LEAK_RE = re.compile(
	r"xpath|css=|locator\(|querySelector|get_by_|//\*|\[id=|\[name=|<[a-z]+[ >]", re.IGNORECASE
)
_STEP_VERDICTS = ("covered", "missing", "unverifiable")

# Regex fallback only. Matches "<role> as <value>" phrases, e.g. "city as SF".
_AS_VALUE_RE = re.compile(r"\bas\s+([^,\s]+)", re.IGNORECASE)
_ROLE_AS_RE = re.compile(r"([^,]+?)\s+as\s+([^,\s]+)", re.IGNORECASE)

# input_parameters keys must be valid Python identifiers (Optexity schema
# checks `key.isidentifier()`) and not shadow the engine's own variables.
_RESERVED_PARAM_NAMES = {"current_page_url", "current_time", "task_id"}

# Prefer attributes that survive a page reload. Never use LLM index here.
_IDENTITY_ATTR_ORDER = ("name", "id", "placeholder")
# Playwright's get_by_test_id maps to data-testid; other hooks use attribute selectors.
_TEST_ID_ATTRS = ("data-testid", "data-test-id")
_DATA_HOOK_ATTRS = ("data-test", "data-cy", "data-qa")
_TAG_TO_ROLE = {
	"a": "link",
	"button": "button",
	"input": "textbox",
	"select": "combobox",
	"textarea": "textbox",
}

# What the page calls this field, used to match a task role ("city") to a box.
_LABEL_ATTR_ORDER = ("aria-label", "title", "placeholder")
_LABEL_LOOKUP_DEPTH = 4
_LABEL_MAX_LEN = 60
_NON_LABEL_TAGS = {"input", "select", "textarea", "button", "script", "style", "svg"}
_ROLE_STOPWORDS = {"the", "a", "an", "of", "field", "box"}
_SENSITIVE_INPUT_TYPES = frozenset({"password"})


def is_sensitive_field(record: dict[str, Any]) -> bool:
	"""Password and credential fields: keep the locator, never persist the typed value."""
	attrs = record.get("attributes") or {}
	input_type = (attrs.get("type") or "").strip().lower()
	if input_type in _SENSITIVE_INPUT_TYPES:
		return True
	label = (record.get("label") or "").strip().lower()
	if "password" in label:
		return True
	ident = record.get("identity") or {}
	by = (ident.get("by") or "").strip().lower()
	value = (ident.get("value") or "").strip().lower()
	if by in {"name", "id", "placeholder"} and "password" in value:
		return True
	for key in ("name", "id", "placeholder", "data-test"):
		attr = (attrs.get(key) or "").strip().lower()
		if "password" in attr:
			return True
	return False


def redact_sensitive_record(record: dict[str, Any]) -> dict[str, Any]:
	"""Strip credential payloads before a row is written or sent to a model digest."""
	out = dict(record)
	if out.get("action") == "input" and is_sensitive_field(out):
		out["text"] = None
		after = out.get("after")
		if isinstance(after, dict):
			after = dict(after)
			target = after.get("target")
			if isinstance(target, dict):
				target = dict(target)
				target["value"] = None
				after["target"] = target
			out["after"] = after
	return out


def cache_path() -> Path:
	raw = os.environ.get("ACTION_CACHE_PATH")
	return Path(raw) if raw else _DEFAULT_PATH


def dispatch_log_path() -> Path:
	raw = os.environ.get("ACTION_DISPATCH_LOG")
	return Path(raw) if raw else _DISPATCH_LOG_PATH


def run_review_path() -> Path:
	raw = os.environ.get("RUN_REVIEW_PATH")
	return Path(raw) if raw else _RUN_REVIEW_PATH


def repair_attempts_path() -> Path:
	raw = os.environ.get("REPAIR_ATTEMPTS_PATH")
	return Path(raw) if raw else _REPAIR_ATTEMPTS_PATH


def automation_json_path() -> Path:
	raw = os.environ.get("AUTOMATION_JSON")
	return Path(raw) if raw else _AUTOMATION_JSON


def _shadow_hosts(node: EnhancedDOMTreeNode) -> list[dict[str, str]]:
	hosts: list[dict[str, str]] = []
	current = node.parent_node
	seen = 0
	while current is not None and seen < 40:
		if getattr(current, "is_shadow_host", False) or current.shadow_roots:
			hosts.append(
				{
					"tag": current.tag_name,
					"name": current.attributes.get("name", ""),
					"id": current.attributes.get("id", ""),
				}
			)
		current = current.parent_node
		seen += 1
	hosts.reverse()
	return hosts


def stable_identity(
	attributes: dict[str, str] | None,
	xpath: str = "",
	shadow_hosts: list[dict[str, str]] | None = None,
) -> dict[str, Any] | None:
	"""Map a live node to a key that should still work on the next run.

	Does not build a Playwright command (that's Stage 5). Returns None if
	we have no stable handle at all.
	"""
	attrs = attributes or {}
	for attr in _IDENTITY_ATTR_ORDER:
		value = (attrs.get(attr) or "").strip()
		if value:
			return {"by": attr, "value": value}
	for attr in _TEST_ID_ATTRS:
		value = (attrs.get(attr) or "").strip()
		if value:
			return {"by": "data-testid", "value": value}
	for attr in _DATA_HOOK_ATTRS:
		value = (attrs.get(attr) or "").strip()
		if value:
			return {"by": attr, "value": value}
	if shadow_hosts:
		host = next((h for h in reversed(shadow_hosts) if h.get("name") or h.get("id")), None)
		if host:
			return {
				"by": "shadow_host",
				"value": host.get("name") or host.get("id"),
				"tag": host.get("tag"),
			}
	if xpath.strip():
		return {"by": "xpath", "value": xpath}
	return None


def resolve_identity(record: dict[str, Any]) -> dict[str, Any] | None:
	"""Best durable handle for a cache row, using everything the hook captured.

	Rows written before test-hook priority existed may still carry xpath as
	`identity`; this re-reads `attributes` at compile time so old traces upgrade
	without another site run.
	"""
	ident = stable_identity(
		record.get("attributes") or {},
		record.get("xpath") or "",
		record.get("shadow_hosts") or [],
	)
	if ident and ident.get("by") != "xpath":
		return ident
	label = _clean_label(record.get("label"))
	tag = (record.get("tag") or "").lower()
	role = _TAG_TO_ROLE.get(tag)
	if role and label and 3 <= len(label) <= _LABEL_MAX_LEN and not label.isdigit():
		return {"by": "role", "role": role, "value": label}
	return ident


def _clean_label(raw: str | None) -> str:
	text = re.sub(r"\s+", " ", (raw or "")).strip().strip(":*").strip()
	return text[:_LABEL_MAX_LEN]


def _node_text(node: EnhancedDOMTreeNode) -> str:
	"""Visible text of a sibling. Other form controls are not labels."""
	try:
		if node.node_type == NodeType.TEXT_NODE:
			return node.node_value or ""
		if node.node_type != NodeType.ELEMENT_NODE:
			return ""
		if (node.tag_name or "").lower() in _NON_LABEL_TAGS:
			return ""
		return node.get_all_children_text(max_depth=3)
	except Exception:
		return ""


def field_label(node: EnhancedDOMTreeNode) -> str:
	"""What the page calls this field, e.g. "City" vs "State".

	Identity ("13adr_city") says which box. Label says which *job* the box does,
	so a task role can be matched against a real field instead of guessing from
	the typed string.
	"""
	try:
		ax_node = getattr(node, "ax_node", None)
		accessible_name = _clean_label(getattr(ax_node, "name", None))
		if accessible_name:
			return accessible_name

		attributes = node.attributes or {}
		for attr in _LABEL_ATTR_ORDER:
			cleaned = _clean_label(attributes.get(attr))
			if cleaned:
				return cleaned

		# Fall back to the nearest preceding text, e.g. <div>City:</div><div><input/></div>
		current: EnhancedDOMTreeNode | None = node
		for _ in range(_LABEL_LOOKUP_DEPTH):
			parent = current.parent_node if current else None
			if parent is None:
				break
			siblings = parent.children_nodes or []
			# Identity compare: dataclass __eq__ would walk the whole subtree.
			position = next((i for i, s in enumerate(siblings) if s is current), len(siblings))
			for sibling in reversed(siblings[:position]):
				cleaned = _clean_label(_node_text(sibling))
				if cleaned:
					return cleaned
			current = parent
	except Exception:
		logger.debug("field_label lookup failed", exc_info=True)
	return ""


def node_identity(node: EnhancedDOMTreeNode) -> dict[str, Any]:
	xpath = ""
	try:
		xpath = node.xpath
	except Exception:
		xpath = ""
	attributes = dict(node.attributes or {})
	shadow_hosts = _shadow_hosts(node)
	return {
		"tag": node.tag_name,
		"attributes": attributes,
		"xpath": xpath,
		"label": field_label(node),
		"shadow_hosts": shadow_hosts,
		"identity": stable_identity(attributes, xpath, shadow_hosts),
	}


# Read once, immediately after the action, while the page is still in front of us.
# Cheap and site-agnostic: three page-wide numbers plus the state of the element we
# just touched. Compared against the previous action's reading, this is what tells
# "the click worked" apart from "the click was dispatched" — the distinction that let
# ten recorded add-to-cart successes sit next to an empty cart.
_EFFECT_PROBE = """
(() => {
  const ident = __IDENT__;
  let el = null;
  try {
    if (ident && ident.by === 'name') el = document.querySelector('[name=' + JSON.stringify(String(ident.value)) + ']');
    else if (ident && ident.by === 'id') el = document.getElementById(String(ident.value));
    else if (ident && ident.by === 'placeholder') el = document.querySelector('[placeholder=' + JSON.stringify(String(ident.value)) + ']');
    else if (ident && ident.by === 'xpath') {
      const xp = String(ident.value);
      el = document.evaluate(xp.startsWith('/') ? xp : '/' + xp, document, null, 9, null).singleNodeValue;
    }
  } catch (e) { el = null; }
  const body = document.body;
  return {
    title: document.title || '',
    interactive: document.querySelectorAll('a,button,input,select,textarea,[role="button"]').length,
    text_len: body ? (body.innerText || '').length : 0,
    target: el
      ? {
          present: true,
          text: ((el.innerText || el.textContent || '').trim()).slice(0, 80),
          value: el.value === undefined || el.value === null ? null : String(el.value).slice(0, 160),
          name: el.getAttribute ? el.getAttribute('name') : null,
        }
      : { present: false },
  };
})()
"""


# A framework re-render is quick but not instant — saucedemo renames its add-to-cart
# button 18ms after the click. A probe fired straight down an already-open CDP channel
# can beat that, read the pre-update DOM, and report a working click as having done
# nothing. Losing that race produces false "no effect" verdicts, which the reviewer is
# told to read as "the step did not happen". Pause long enough to lose to no re-render.
_PROBE_SETTLE_SECONDS = 0.3


async def capture_effect(browser_session: Any, node: EnhancedDOMTreeNode | None) -> dict[str, Any] | None:
	"""Page and target state right after an action. Never raises into the agent."""
	try:
		await asyncio.sleep(_PROBE_SETTLE_SECONDS)
		identity = node_identity(node)["identity"] if node is not None else None
		script = _EFFECT_PROBE.replace("__IDENT__", json.dumps(identity))
		cdp_session = await browser_session.get_or_create_cdp_session(target_id=None, focus=False)
		result = await cdp_session.cdp_client.send.Runtime.evaluate(
			params={"expression": script, "returnByValue": True},
			session_id=cdp_session.session_id,
		)
		value = (result or {}).get("result", {}).get("value")
		return value if isinstance(value, dict) else None
	except Exception:
		logger.debug("effect probe failed", exc_info=True)
		return None


def append_executed_action(
	*,
	action: str,
	index: int | None,
	text: str | None,
	node: EnhancedDOMTreeNode | None,
	url: str,
	after: dict[str, Any] | None = None,
) -> None:
	"""Append one executed action. Never raise into the agent loop."""
	try:
		record: dict[str, Any] = {
			"t": datetime.now(timezone.utc).isoformat(),
			"action": action,
			"index": index,
			"text": text,
			"url": url,
		}
		if after is not None:
			record["after"] = after
		if node is not None:
			record.update(node_identity(node))
		else:
			record["identity"] = None
		record = redact_sensitive_record(record)
		path = cache_path()
		path.parent.mkdir(parents=True, exist_ok=True)
		with path.open("a", encoding="utf-8") as f:
			f.write(json.dumps(record, ensure_ascii=False) + "\n")
		logger.info(
			"action_cache wrote %s index=%s identity=%s -> %s",
			action,
			index,
			record.get("identity"),
			path,
		)
	except Exception:
		logger.exception("action_cache write failed")


# Event types that mutate the page AND have an Optexity node equivalent, so they
# are worth recording. Anything else (extract, wait, scroll, tab bookkeeping) is
# not replayable, so not recording it is a decision rather than a gap.
_REPLAYABLE_EVENTS = {
	"TypeTextEvent": "input",
	"ClickElementEvent": "click",
	"NavigateToUrlEvent": "go_to_url",
	"GoBackEvent": "go_back",
	"SelectDropdownOptionEvent": "select_option",
	"UploadFileEvent": "upload_file",
	"SendKeysEvent": "key_press",
}


def _note_dispatched_event(event: Any) -> None:
	"""Count replayable actions the agent asked for. Never writes a cache row.

	The bus runs handlers in parallel with the real one, so this cannot know
	whether the action succeeded. That is why it only audits coverage while the
	recorder stays at execute-time, where success is certain.
	"""
	try:
		event_type = getattr(event, "event_type", "") or type(event).__name__
		if event_type not in _REPLAYABLE_EVENTS:
			return
		path = dispatch_log_path()
		path.parent.mkdir(parents=True, exist_ok=True)
		with path.open("a", encoding="utf-8") as f:
			f.write(json.dumps({"t": datetime.now(timezone.utc).isoformat(), "event": event_type}) + "\n")
	except Exception:
		logger.debug("hook audit write failed", exc_info=True)


def install_hook_audit(event_bus: Any) -> None:
	"""Attach the coverage auditor. Idempotent; the bus is recreated on reset."""
	try:
		already_attached = any(
			getattr(handler, "__name__", "") == "_note_dispatched_event"
			for handler in event_bus.handlers.get("*", [])
		)
		if already_attached:
			return
		event_bus.on("*", _note_dispatched_event)
		logger.info("action_cache hook audit attached -> %s", dispatch_log_path())
	except Exception:
		logger.debug("could not attach hook audit", exc_info=True)


def hook_coverage(
	dispatch_path: Path | None = None,
	cache_src: Path | None = None,
) -> dict[str, Any]:
	"""Which replayable actions the agent used vs which ones the hook recorded."""
	dispatched = Counter(
		record.get("event") for record in read_jsonl(dispatch_path or dispatch_log_path())
	)
	recorded = Counter(record.get("action") for record in read_jsonl(cache_src or cache_path()))
	used: list[dict[str, Any]] = []
	blind_spots: list[str] = []
	for event_type, action in sorted(_REPLAYABLE_EVENTS.items()):
		dispatched_count = dispatched.get(event_type, 0)
		if not dispatched_count:
			continue
		recorded_count = recorded.get(action, 0)
		used.append(
			{
				"event": event_type,
				"action": action,
				"dispatched": dispatched_count,
				"recorded": recorded_count,
				"hooked": recorded_count > 0,
			}
		)
		if recorded_count == 0:
			blind_spots.append(event_type)
	return {
		"audit_available": bool(dispatched),
		"used_by_workflow": used,
		"blind_spots": blind_spots,
		"next": (
			"Blind spot = the agent used this action but nothing was recorded. "
			"Hook it at execute-time; do not reconstruct a locator afterwards."
		),
	}


def write_hook_coverage(report: dict[str, Any], path: Path | None = None) -> Path:
	path = path or _HOOK_COVERAGE_PATH
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
	logger.info("hook coverage: blind_spots=%s -> %s", report.get("blind_spots"), path)
	return path


def load_agentic_task(automation_path: Path | None = None) -> str:
	path = automation_path or automation_json_path()
	if path.exists():
		try:
			data = json.loads(path.read_text(encoding="utf-8"))
			return (
				data.get("nodes", [{}])[0]
				.get("interaction_action", {})
				.get("agentic_task", {})
				.get("task", "")
			) or ""
		except Exception:
			logger.exception("could not read agentic task from %s", path)
	return ""


def task_values_from_text(task: str) -> tuple[str, ...]:
	"""Regex fallback: pull fill-values from phrases like 'as myname' / 'as SF'.

	Returns exactly what matched — including nothing. Never substitutes an
	unrelated default; a caller that got nothing here has to decide what that
	means, not have this function guess for it.
	"""
	return tuple(m.group(1).strip().strip("\"'") for m in _AS_VALUE_RE.finditer(task or ""))


def task_roles_from_text(task: str) -> dict[str, str]:
	"""Regex fallback: map value -> role phrase ('city as SF' -> SF: 'city')."""
	roles: dict[str, str] = {}
	for match in _ROLE_AS_RE.finditer(task or ""):
		role = re.sub(r"^(fill\s+(the\s+)?|and\s+)", "", match.group(1).strip(), flags=re.I).strip()
		value = match.group(2).strip().strip("\"'")
		if value:
			roles[value] = role
	return roles


def load_task_values(automation_path: Path | None = None) -> tuple[str, ...]:
	"""Regex-only required values. Legacy path — prefer `required_values_and_roles`.

	No longer substitutes Roboform's old values when nothing matches. That
	silent swap is exactly what made a bad filter look like a working one on
	any task not phrased "role as value" (see Open Items.md).
	"""
	return task_values_from_text(load_agentic_task(automation_path))


_RUN_REVIEW_SYSTEM_PROMPT = (
	"You audit a finished browser-automation run.\n\n"
	"You get the TASK the agent was asked to do, and the TRACE of the actions that were "
	"actually recorded while it ran. Decide, per step, whether the trace shows that step "
	"happening.\n\n"
	"A trace entry records that an action was ATTEMPTED. Where we could measure what it "
	"did, the entry also carries `effect`:\n"
	"- `effective` — the page provably changed (see `effect_detail`). Strong evidence the "
	"step really happened.\n"
	"- `no_observed_effect` — the action ran and the page did not change at all. Treat this "
	"as evidence the step did NOT happen, however promising the label looks.\n"
	"- `no_navigation` — a click that did not change the page URL. It may still have had an "
	"in-page effect we could not measure, so this is weak evidence, not proof of failure.\n"
	"- absent — we could not measure it. Judge from the label alone.\n"
	"Prefer citing an `effective` entry over an identical-looking one without it.\n\n"
	"Return ONLY one JSON object, no prose, no markdown fence:\n"
	'{"values": [{"role": "<what the field is for>", "value": "<literal text to type>"}],\n'
	' "steps": [{"description": "<the step, in the task\'s own words>",\n'
	'            "kind": "click|input|navigate|other",\n'
	'            "expected_target": "<visible text to look for, e.g. Add to basket>",\n'
	'            "verdict": "covered|missing|unverifiable",\n'
	'            "evidence": <index of the trace entry that performed it, or null>,\n'
	'            "repair_prompt": "<how to retry a missing step, else null>"}]}\n\n'
	"Rules:\n"
	"- values: every literal value the TASK asks to be typed into a field, empty array if "
	"the task is pure navigation. Never invent a value the task does not state.\n"
	"- steps: split the TASK into the distinct browser actions it asks for, in order.\n"
	'- "covered" REQUIRES an evidence index pointing at the trace entry that performed it. '
	"No index means not covered. Never cite the same index for two steps.\n"
	'- "unverifiable" when the trace is too vague to tell (no label, ambiguous match).\n'
	'- "missing" only when you are confident no trace entry performs that step.\n'
	"- repair_prompt (missing steps only): tell a browser agent what to do in human terms — "
	"visible button text, where on the page, what to avoid. NEVER write an xpath, a CSS "
	"selector, an element index, or code. Describe only what a person would see."
)


def _extract_json_object(text: str) -> Any:
	cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
	try:
		return json.loads(cleaned)
	except json.JSONDecodeError:
		start, end = cleaned.find("{"), cleaned.rfind("}")
		if start == -1 or end == -1 or end < start:
			raise
		return json.loads(cleaned[start : end + 1])


def trace_digest(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Locator-free view of a run, for the reviewing model.

	Deliberately drops xpath, identity and command. A model that never sees a
	locator cannot leak one into a repair prompt, so "never invent a locator"
	holds by construction instead of by asking the model nicely.
	"""
	digest = []
	for i, record in enumerate(records):
		row = redact_sensitive_record(record)
		entry = {
			"i": i,
			"action": row.get("action"),
			"label": (row.get("label") or "").strip(),
			"text": row.get("text"),
			"url": row.get("url"),
		}
		# What the action *did*, when we know. Without this the model can only see
		# that something was attempted, which is why ten dead clicks on "Add to cart"
		# read exactly like ten successful ones.
		effect = record.get("effect") or {}
		if effect.get("verdict") and effect["verdict"] != "unknown":
			entry["effect"] = effect["verdict"]
			entry["effect_detail"] = effect.get("why")
		digest.append(entry)
	return digest


def llm_review_run(task: str, digest: list[dict[str, Any]], model: str | None = None) -> dict[str, Any]:
	"""One post-run call: task + trace -> required values and per-step verdicts.

	Runs after the agent has finished, so the model can do the one thing it is
	genuinely better at than string matching — deciding that "the Travel
	category" in the task and the label "Travel" in the log are the same thing.
	It proposes verdicts; it does not get the last word (see `_validated_review`).
	"""
	import litellm

	model = model or os.environ.get("LLM_MODEL")
	if not model:
		raise RuntimeError("LLM_MODEL not set; cannot run the post-run review")
	response = litellm.completion(
		model=model,
		api_key=os.environ.get("LLM_MODEL_API_KEY"),
		temperature=0,
		drop_params=True,
		num_retries=0,
		messages=[
			{"role": "system", "content": _RUN_REVIEW_SYSTEM_PROMPT},
			{
				"role": "user",
				"content": json.dumps({"task": task, "trace": digest}, ensure_ascii=False, indent=2),
			},
		],
	)
	parsed = _extract_json_object(response.choices[0].message.content or "")
	if not isinstance(parsed, dict):
		raise ValueError(f"run review returned non-object: {parsed!r}")
	return parsed


def _scrub_repair_prompt(prompt: Any) -> tuple[str, bool]:
	"""Drop any repair prompt carrying a selector. Returns (text, leaked)."""
	text = str(prompt or "").strip()
	if not text:
		return "", False
	if _LOCATOR_LEAK_RE.search(text):
		return "", True
	return text, False


def _empty_step_summary(digest: list[dict[str, Any]], available: bool) -> dict[str, Any]:
	return {
		"available": available,
		"steps_total": 0,
		"covered": 0,
		"missing": 0,
		"unverifiable": 0,
		"actions_recorded": len(digest),
		"conflicts": [],
	}


def _validated_review(raw: dict[str, Any], digest: list[dict[str, Any]]) -> dict[str, Any]:
	"""Hold the model's verdicts to evidence it can actually point at.

	The model is the only thing here that can read a sentence, but it is also
	the only thing that can claim a step happened when it did not. So every
	"covered" has to name a trace row, a row can only be spent once, and the
	row count is checked independently — a claim that outruns the log loses.
	"""
	values: list[dict[str, str]] = []
	for item in raw.get("values") or []:
		if not isinstance(item, dict):
			continue
		value = str(item.get("value", "")).strip()
		if value:
			values.append({"role": str(item.get("role", "")).strip(), "value": value})

	steps: list[dict[str, Any]] = []
	claimed_by: dict[int, int] = {}
	for position, item in enumerate(raw.get("steps") or []):
		if not isinstance(item, dict):
			continue
		description = str(item.get("description", "")).strip()
		if not description:
			continue
		target = str(item.get("expected_target", "")).strip()
		kind = str(item.get("kind", "")).strip().lower()
		kind = kind if kind in ("click", "input", "navigate", "other") else "other"
		verdict = str(item.get("verdict", "")).strip().lower()
		verdict = verdict if verdict in _STEP_VERDICTS else "unverifiable"

		evidence = item.get("evidence")
		if not isinstance(evidence, int) or not 0 <= evidence < len(digest):
			evidence = None
		notes: list[str] = []
		if verdict == "covered" and evidence is None:
			verdict = "unverifiable"
			notes.append("claimed covered without pointing at a trace row")
		if evidence is not None and evidence in claimed_by:
			notes.append(f"trace[{evidence}] was already claimed by step {claimed_by[evidence] + 1}")
			verdict, evidence = "unverifiable", None
		if evidence is not None:
			claimed_by[evidence] = position

		repair, leaked = _scrub_repair_prompt(item.get("repair_prompt"))
		repair_source = "llm" if repair else None
		if leaked:
			notes.append("repair prompt dropped: it contained a selector")
		if verdict == "missing" and not repair:
			repair = f"Do this step, which the previous run did not complete: {description}."
			if target:
				repair += f" Look for the element labelled {target!r} and act on that one only."
			repair_source = "template"

		steps.append(
			{
				"description": description,
				"kind": kind,
				"expected_target": target,
				"verdict": verdict,
				"evidence": evidence,
				"evidence_label": digest[evidence]["label"] if evidence is not None else "",
				"repair_prompt": repair or None,
				"repair_prompt_source": repair_source,
				"notes": notes,
			}
		)

	summary = _empty_step_summary(digest, available=True)
	summary["steps_total"] = len(steps)
	for state in _STEP_VERDICTS:
		summary[state] = sum(1 for s in steps if s["verdict"] == state)
	# Model-independent floor: N steps cannot fit into fewer than N recorded
	# actions, whatever the verdicts say. Reporting less than that floor as
	# missing means the review is under-counting (often by hiding a real hole
	# under "unverifiable"). Caveat in the message because an action type we
	# never hooked also lands here — see hook_coverage.json.
	shortfall = summary["steps_total"] - len(digest)
	if shortfall > summary["missing"]:
		summary["conflicts"].append(
			f"at least {shortfall} step(s) cannot have happened ({summary['steps_total']} steps vs "
			f"{len(digest)} recorded actions) but only {summary['missing']} reported missing — "
			"check hook_coverage.json for an action type that is never recorded"
		)
	if steps and not digest:
		summary["conflicts"].append("no actions were recorded at all")
	return {"values": values, "steps": steps, "summary": summary}


def _regex_task_spec(task: str) -> list[dict[str, str]]:
	roles = task_roles_from_text(task)
	return [{"role": roles.get(v, ""), "value": v} for v in task_values_from_text(task)]


def _canonical_digest(digest: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Fingerprint input: password rows never carry literal text."""
	out: list[dict[str, Any]] = []
	for entry in digest:
		row = dict(entry)
		if (row.get("label") or "").strip().lower() == "password":
			row["text"] = None
		out.append(row)
	return out


def _review_fingerprint(task: str, digest: list[dict[str, Any]]) -> str:
	blob = json.dumps(
		{"task": task, "trace": _canonical_digest(digest)},
		ensure_ascii=False,
		sort_keys=True,
	)
	return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_run_review(
	automation_path: Path | None = None,
	cache_src: Path | None = None,
	review_path: Path | None = None,
	force: bool = False,
) -> dict[str, Any]:
	"""Post-run review, cached to `run_logs/run_review.json`.

	Keyed on task text *and* the trace, so a recompile is free but a gap-fill
	run that adds records invalidates the verdict automatically — which is what
	makes the repair loop terminate on evidence rather than on optimism.

	If the LLM is unavailable the values fall back to regex and the step check
	reports itself unavailable. It never reports "0 missing" when it did not
	actually look.
	"""
	task = load_agentic_task(automation_path)
	digest = trace_digest(read_jsonl(cache_src or cache_path()))
	path = review_path or run_review_path()
	fingerprint = _review_fingerprint(task, digest)

	if not force and path.exists():
		try:
			cached = json.loads(path.read_text(encoding="utf-8"))
			if cached.get("fingerprint") == fingerprint:
				return cached
			# Reviews saved before password redaction may still list the literal in
			# `trace`; canonicalize both sides so recompile does not call the LLM again.
			if _review_fingerprint(task, cached.get("trace") or []) == fingerprint:
				return cached
		except Exception:
			logger.warning("run review cache unreadable, regenerating", exc_info=True)

	if not task:
		result: dict[str, Any] = {
			"values": [],
			"steps": [],
			"summary": _empty_step_summary(digest, available=False),
			"source": "none",
		}
	else:
		try:
			result = _validated_review(llm_review_run(task, digest), digest)
			result["source"] = "llm"
		except Exception:
			logger.warning(
				"post-run review failed; values fall back to regex and the step check is unavailable",
				exc_info=True,
			)
			values = _regex_task_spec(task)
			result = {
				"values": values,
				"steps": [],
				"summary": _empty_step_summary(digest, available=False),
				"source": "regex" if values else "none",
			}

	result["task"] = task
	result["fingerprint"] = fingerprint
	result["trace"] = digest
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
	summary = result["summary"]
	logger.info(
		"run review (%s): %s value(s), steps %s covered / %s missing / %s unverifiable of %s -> %s",
		result["source"],
		len(result["values"]),
		summary["covered"],
		summary["missing"],
		summary["unverifiable"],
		summary["steps_total"],
		path,
	)
	for conflict in summary["conflicts"]:
		logger.warning("run review conflict: %s", conflict)
	return result


def required_values_and_roles(automation_path: Path | None = None) -> tuple[tuple[str, ...], dict[str, str]]:
	"""Single source of truth for Stages 3/4/6.5: which values matter and why."""
	spec = load_run_review(automation_path)["values"]
	values = tuple(item["value"] for item in spec)
	roles = {item["value"]: item["role"] for item in spec if item.get("role")}
	return values, roles


def missing_steps(review: dict[str, Any] | None = None) -> list[dict[str, Any]]:
	"""Steps the run never performed, with a usable repair prompt."""
	review = review if review is not None else load_run_review()
	return [
		step
		for step in review.get("steps") or []
		if step.get("verdict") == "missing" and step.get("repair_prompt")
	]


def repair_target_url(review: dict[str, Any] | None = None, automation_path: Path | None = None) -> str:
	"""Where a repair attempt should resume: the furthest page the run reached.

	Read off the last recorded action, so the retry can jump straight there as
	a deterministic initial action instead of paying LLM steps to re-navigate.
	The URL comes from the log; nothing is guessed.
	"""
	review = review if review is not None else load_run_review()
	for row in reversed(review.get("trace") or []):
		if row.get("url"):
			return str(row["url"])
	path = automation_path or automation_json_path()
	if path.exists():
		try:
			return str(json.loads(path.read_text(encoding="utf-8")).get("url") or "")
		except Exception:
			logger.debug("could not read url from %s", path, exc_info=True)
	return ""


def load_repair_attempts(path: Path | None = None) -> dict[str, list[dict[str, Any]]]:
	"""Attempt history keyed by step description, so retries can differ."""
	path = path or repair_attempts_path()
	if not path.exists():
		return {}
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
		return data if isinstance(data, dict) else {}
	except Exception:
		logger.warning("repair attempt history unreadable, starting fresh", exc_info=True)
		return {}


def record_repair_attempt(
	step_description: str,
	attempt: dict[str, Any],
	path: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
	path = path or repair_attempts_path()
	history = load_repair_attempts(path)
	history.setdefault(step_description, []).append(attempt)
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
	return history


_REPAIR_PROMPT_SYSTEM = (
	"You rewrite the instruction given to a browser agent that just failed to finish one "
	"step of a task.\n\n"
	"You get the overall TASK, the STEP still not done, every ATTEMPT already made (the "
	"exact instruction used and what the agent reported afterwards), and PAGE_ELEMENTS — "
	"the interactive elements actually present on the page, as the page itself labels "
	"them. PROPOSED_INSTRUCTION, when present, is the wording that would be used next and "
	"has NOT been tried yet.\n\n"
	"Every attempt listed FAILED — that is a fact, established by the absence of any "
	"recorded browser action for this step. `agent_reported` is the agent's own account "
	"and is sometimes confidently wrong, including claiming success; treat "
	"`recorded_a_new_action: false` as the truth and the prose as a hint about what the "
	"agent could see.\n\n"
	"Write ONE new instruction that is materially different from every previous attempt and "
	"that addresses the specific reason the last attempt failed. With no attempts yet, "
	"rewrite PROPOSED_INSTRUCTION so it names elements the way PAGE_ELEMENTS names them, "
	"keeping its intent.\n\n"
	"Rules:\n"
	"- Refer to elements using wording that appears in PAGE_ELEMENTS. That list is the "
	"page's own vocabulary; anything not in it is a guess.\n"
	"- Do NOT describe colours, sizes, or positions unless an attempt reported them. If you "
	"do not know where something sits, say to scroll until the named text is visible.\n"
	"- NEVER write an xpath, a CSS selector, an element index, or code.\n"
	"- Do not restate a previous instruction.\n"
	"- If PAGE_ELEMENTS shows several elements with the same label, say plainly how to tell "
	"the right one apart using neighbouring text from that list.\n"
	"- Return ONLY the instruction text: no JSON, no quotes, no preamble."
)

def page_landmarks(selector_map: dict[int, Any], limit: int = 120) -> list[dict[str, str]]:
	"""The page's own vocabulary: what each interactive element is called.

	Labels only — no xpath, no index, no command. This is what a repair prompt is
	allowed to refer to, so an instruction can name a real element instead of a
	model's guess about one. Duplicates are kept with a count, because "there are
	four things called Add to basket" is precisely the ambiguity a prompt has to
	resolve.
	"""
	counts: Counter[tuple[str, str]] = Counter()
	for node in (selector_map or {}).values():
		try:
			label = field_label(node)
		except Exception:
			label = ""
		if not label:
			continue
		counts[((node.tag_name or "").lower(), label)] += 1
	landmarks = [
		{"tag": tag, "label": label, "count": str(count)}
		for (tag, label), count in counts.most_common(limit)
	]
	return landmarks


def target_present(target: str, landmarks: list[dict[str, str]] | None) -> bool:
	"""Is the thing we are looking for even on this page?"""
	needle = _normalize_phrase(target)
	if not needle or not landmarks:
		return False
	for landmark in landmarks:
		label = _normalize_phrase(landmark.get("label", ""))
		if label and (needle in label or label in needle):
			return True
	return False


def _fallback_repair_prompt(step: dict[str, Any], attempts: list[dict[str, Any]]) -> str:
	"""Generic sharpening that invents no page facts."""
	target = (step.get("expected_target") or "").strip()
	tried = (
		f"{len(attempts)} earlier attempt(s) with different wording did not achieve this. "
		if attempts
		else ""
	)
	text = (
		f"{step['description']}. "
		f"{tried}"
		"Scroll through the whole page first, then act on the element in the main content "
		"area rather than in any sidebar, footer, or recommendation strip."
	)
	if target:
		text += f" The element you want shows the text {target!r}."
	return text


def improve_repair_prompt(
	task: str,
	step: dict[str, Any],
	attempts: list[dict[str, Any]],
	landmarks: list[dict[str, str]] | None = None,
	baseline: str | None = None,
	model: str | None = None,
) -> dict[str, Any]:
	"""Next instruction for a step that keeps failing.

	Fed the previous instructions *and what happened when they ran* — without
	that, the same inputs produce the same prompt and the loop spins forever
	re-issuing an instruction already known not to work.

	`baseline` is an untried instruction to ground rather than replace, which is
	what the very first repair has instead of a failure to learn from.

	Returns the prompt plus `repeated`, which the caller uses as a no-progress
	signal: an unchanged instruction is not a retry, it is a loop.
	"""
	previous = [a.get("prompt", "") for a in attempts]
	try:
		import litellm

		resolved = model or os.environ.get("LLM_MODEL")
		if not resolved:
			raise RuntimeError("LLM_MODEL not set; cannot sharpen the repair prompt")
		response = litellm.completion(
			model=resolved,
			api_key=os.environ.get("LLM_MODEL_API_KEY"),
			temperature=0,
			drop_params=True,
			num_retries=0,
			messages=[
				{"role": "system", "content": _REPAIR_PROMPT_SYSTEM},
				{
					"role": "user",
					"content": json.dumps(
						{
							"task": task,
							"step": {
								"description": step.get("description"),
								"expected_target": step.get("expected_target"),
								"kind": step.get("kind"),
							},
							"attempts": [
								{
									"instruction": a.get("prompt"),
									"recorded_a_new_action": bool(a.get("new_records")),
									"agent_claimed_success": a.get("agent_claimed_success"),
									"agent_reported": a.get("agent_report"),
								}
								for a in attempts
							],
							"page_elements": landmarks or [],
							**({"proposed_instruction": baseline} if baseline and not attempts else {}),
						},
						ensure_ascii=False,
						indent=2,
					),
				},
			],
		)
		raw = (response.choices[0].message.content or "").strip().strip("`").strip()
		prompt, leaked = _scrub_repair_prompt(raw)
		source = "llm"
		if leaked or not prompt:
			prompt, source = _fallback_repair_prompt(step, attempts), "template"
	except Exception:
		logger.warning("repair prompt sharpening failed, using template", exc_info=True)
		prompt, source = _fallback_repair_prompt(step, attempts), "template"

	normalized = _normalize_phrase(prompt)
	repeated = any(_normalize_phrase(p) == normalized for p in previous)
	return {"prompt": prompt, "source": source, "repeated": repeated}


def click_evidence_indices(review: dict[str, Any]) -> set[int]:
	"""Trace rows the step reviewer credited to a task step (covered or unverifiable)."""
	indices: set[int] = set()
	for step in review.get("steps") or []:
		evidence = step.get("evidence")
		if isinstance(evidence, int) and step.get("verdict") in ("covered", "unverifiable"):
			indices.add(evidence)
	return indices


def _click_on_task(
	record: dict[str, Any],
	raw_index: int | None,
	review: dict[str, Any] | None,
) -> bool:
	"""Stage 3 for clicks: keep only rows the step review pointed at.

	Fails open in every case where the evidence is absent rather than negative,
	for the same reason `allowed` being empty keeps every input: "the reviewer
	told us nothing" is indistinguishable from "the reviewer broke", and
	over-keeping is visible in coverage while over-dropping is silent. A review
	that cites zero trace rows — no steps, or every step `missing` — is treated
	as no information, not as proof that every click was junk.
	"""
	if review is None or not (review.get("summary") or {}).get("available"):
		return True
	if raw_index is None:
		return True
	credited = click_evidence_indices(review)
	if not credited:
		return True
	return raw_index in credited


def is_on_task(
	record: dict[str, Any],
	allowed: tuple[str, ...] | list[str],
	raw_index: int | None = None,
	review: dict[str, Any] | None = None,
) -> bool:
	"""Stage 3: filter inputs by task values; filter clicks by step-review evidence.

	An empty `allowed` means "we don't know what's required" (extraction
	found nothing), not "reject everything typed." Dropping every input when
	the requirement list is unknown is a silent, confident-looking deletion —
	exactly the failure this project already hit once. Keeping everything
	instead is recoverable: Stage 6.5's `extra` field will show whatever
	showed up, for a human to check.
	"""
	action = record.get("action")
	if action == "click":
		return _click_on_task(record, raw_index, review)
	if action != "input":
		return True
	# Locator only — the value comes from input_parameters, not from the log row.
	if is_sensitive_field(record):
		return True
	if not allowed:
		return True
	text = (record.get("text") or "").strip()
	if not text:
		return False
	allowed_set = {v.strip() for v in allowed}
	return text in allowed_set


def read_jsonl(path: Path) -> list[dict[str, Any]]:
	if not path.exists():
		return []
	return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open("w", encoding="utf-8") as f:
		for record in records:
			f.write(json.dumps(record, ensure_ascii=False) + "\n")


def align_cache(
	src: Path | None = None,
	dst: Path | None = None,
	allowed: tuple[str, ...] | None = None,
	review: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
	"""Write on-task records to action_cache_aligned.jsonl. Does not last-write-wins (Stage 4)."""
	src = src or cache_path()
	dst = dst or _ALIGNED_PATH
	allowed = allowed if allowed is not None else required_values_and_roles()[0]
	review = review if review is not None else load_run_review()
	raw = annotate_effects(read_jsonl(src))
	# Click filtering matches `review.trace` positions to rows of this file, and the
	# review is always built from `cache_path()`. Reading a different source would
	# still produce indices, just ones pointing at the wrong actions — so drop the
	# index rule rather than filter confidently against a misaligned trace.
	aligned_to_review = src.resolve() == cache_path().resolve()
	if not aligned_to_review:
		logger.info(
			"stage3 src %s is not the reviewed log; keeping all clicks (no index alignment)",
			src,
		)
	kept: list[dict[str, Any]] = []
	click_dropped = 0
	for index, record in enumerate(raw):
		position = index if aligned_to_review else None
		if is_on_task(record, allowed, raw_index=position, review=review):
			kept.append(record)
		elif record.get("action") == "click":
			click_dropped += 1
			logger.info(
				"stage3 dropped off-task click trace[%s] label=%r identity=%s",
				index,
				record.get("label"),
				record.get("identity"),
			)
	dropped = len(raw) - len(kept)
	write_jsonl(dst, kept)
	logger.info(
		"stage3 align: %s raw -> %s kept, %s dropped (%s off-task clicks), values=%s -> %s",
		len(raw),
		len(kept),
		dropped,
		click_dropped,
		allowed,
		dst,
	)
	return kept


# Ranked best evidence first. `unknown` sits above `no_navigation` because "we could
# not tell" must never be punished as hard as "we looked and saw nothing".
_EFFECT_RANK = {"effective": 2, "unknown": 1, "no_navigation": 1, "no_observed_effect": 0}


def effect_rank(record: dict[str, Any]) -> int:
	"""How much this record proves it did something. Missing data ranks neutral."""
	return _EFFECT_RANK.get((record.get("effect") or {}).get("verdict"), 1)


def action_effect(
	record: dict[str, Any],
	previous: dict[str, Any] | None,
	previous_effect: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Did this action change anything? Judged against the previous action's reading.

	The previous record's `after` doubles as this action's `before`, so one probe per
	action is enough and no pre-action hook is needed. The cost is that a change made
	by something else in between (a redirect, a timer) is attributed here — so this is
	strong evidence, never proof.

	Records written before the probe existed have no `after`, and must come out
	`unknown` rather than `no_observed_effect`: treating absent data as absence of
	effect would condemn every historical trace we still compile from.
	"""
	action = record.get("action") or ""
	after = record.get("after") or {}
	before = (previous or {}).get("after") or {}

	url_changed: bool | None = None
	if previous is not None and record.get("url") and previous.get("url"):
		url_changed = record["url"] != previous["url"]

	# A reading taken immediately after a navigation catches the new page mid-render,
	# so it is useless as a baseline: saucedemo's inventory page measured 1435 characters
	# just after login and 1385 once settled, and charging that 50-character drift to the
	# next click declared a dead "Add to cart" a success. Only compare two readings of
	# the same page, neither of which straddles a navigation.
	comparable = bool(
		after
		and before
		and previous is not None
		and record.get("url") == previous.get("url")
		and (previous_effect or {}).get("url_changed") is not True
	)
	# Structure over prose. Title and interactive-element count survive re-render noise;
	# raw text length does not, so a text-only difference is treated as "cannot tell"
	# rather than as proof either way.
	page_changed = (
		any(after.get(k) != before.get(k) for k in ("title", "interactive")) if comparable else None
	)
	text_changed = after.get("text_len") != before.get("text_len") if comparable else None

	target = after.get("target") if after else None
	target_present = target.get("present") if isinstance(target, dict) else None

	typed_ok: bool | None = None
	if action == "input" and isinstance(target, dict) and record.get("text") is not None:
		typed_ok = (target.get("value") or "") == record["text"]
	elif action == "input" and is_sensitive_field(record) and isinstance(target, dict) and target.get(
		"present"
	):
		# Value is intentionally absent from the log; presence of the field is enough.
		typed_ok = True

	# Only actions that *can* navigate get credit for a navigation. Typing into a field
	# cannot move the page, so when a url change straddles a typing action it came from
	# something else — a new pass starting at the login screen, in the trace that caught
	# this. Crediting it there would manufacture evidence for a step nobody performed.
	navigable = action in {"click", "navigate", "go_back", "send_keys"}

	if url_changed and navigable:
		verdict, why = "effective", "the page navigated"
	elif typed_ok:
		verdict, why = "effective", "the field holds the typed value"
	elif action == "click" and target_present is False:
		verdict, why = "effective", "the clicked element was replaced or removed"
	elif page_changed:
		verdict, why = "effective", "the page structure changed"
	elif comparable and text_changed:
		verdict, why = "unknown", "only the page text length changed, too weak to attribute"
	elif comparable:
		verdict, why = "no_observed_effect", "the page did not change at all"
	elif action == "click" and url_changed is False:
		verdict, why = "no_navigation", "the page did not navigate; in-page effects unmeasured"
	else:
		verdict, why = "unknown", "no effect data captured for this action"

	return {
		"verdict": verdict,
		"why": why,
		"url_changed": url_changed,
		"page_changed": page_changed,
		"target_present_after": target_present,
		"typed_value_present": typed_ok,
	}


def annotate_effects(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Add `effect` to every record, comparing each against the one before it.

	Must run on the *raw* consecutive trace: filtering first would break adjacency
	and make the url comparison compare two actions that never followed each other.
	"""
	out: list[dict[str, Any]] = []
	previous: dict[str, Any] | None = None
	previous_effect: dict[str, Any] | None = None
	for record in records:
		annotated = dict(record)
		annotated["effect"] = action_effect(record, previous, previous_effect)
		out.append(annotated)
		previous = record
		previous_effect = annotated["effect"]
	counts = Counter(r["effect"]["verdict"] for r in out)
	if out:
		logger.info(
			"effects: %s",
			", ".join(f"{verdict}={count}" for verdict, count in sorted(counts.items())),
		)
	return out


def identity_key(record: dict[str, Any]) -> tuple[str, ...] | None:
	"""Stable field key: action + identity (not LLM index)."""
	ident = record.get("identity") or {}
	by = ident.get("by")
	value = ident.get("value")
	if not by or not value:
		return None
	return (record.get("action") or "", by, str(value))


def last_write_per_identity(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Stage 4: one record per field. First-seen order, last successful write wins."""
	last: dict[tuple[str, ...], dict[str, Any]] = {}
	order: list[tuple[str, ...]] = []
	skipped = 0
	for record in records:
		key = identity_key(record)
		if key is None:
			skipped += 1
			continue
		if key not in last:
			order.append(key)
			last[key] = record
			continue
		# Among repeats of the same element, prefer the attempt that demonstrably did
		# something. `>=` keeps last-write-wins on equal evidence, so a trace with no
		# effect data behaves exactly as it did before this rule existed.
		if effect_rank(record) >= effect_rank(last[key]):
			last[key] = record
	logger.info(
		"stage4 slice: %s in -> %s unique identities, %s skipped (no identity)",
		len(records),
		len(order),
		skipped,
	)
	return [last[k] for k in order]


def _normalize_phrase(text: str) -> str:
	return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()


def role_match_score(role: str, label: str) -> int:
	"""How well a field's on-page label matches the role the task asked for.

	3 exact, 2 one contains the other, 1 shared word, 0 no evidence either way.
	Score 0 also means "no label captured", so callers must fail open.
	"""
	normalized_role = _normalize_phrase(role)
	normalized_label = _normalize_phrase(label)
	if not normalized_role or not normalized_label:
		return 0
	if normalized_role == normalized_label:
		return 3
	if normalized_role in normalized_label or normalized_label in normalized_role:
		return 2
	role_words = set(normalized_role.split()) - _ROLE_STOPWORDS
	label_words = set(normalized_label.split()) - _ROLE_STOPWORDS
	return 1 if role_words & label_words else 0


def best_write_per_input_text(
	records: list[dict[str, Any]],
	roles: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
	"""If one task value was typed into two boxes, keep the box the task meant.

	Retries on a single field are already collapsed by identity. This handles
	the harder case: "SF" is a valid city *and* a valid state, so two different
	fields accept it. Ranking by label match answers "which field is City?"
	instead of "which SF was typed last?".

	Falls back to last-write-wins when no labels were captured, so records from
	older runs still compile.
	"""
	roles = roles if roles is not None else required_values_and_roles()[1]
	best: dict[str, tuple[int, int]] = {}
	keep: set[int] = set()
	for position, record in enumerate(records):
		if record.get("action") != "input":
			continue
		if is_sensitive_field(record):
			keep.add(position)
			continue
		text = (record.get("text") or "").strip()
		if not text:
			continue
		score = role_match_score(roles.get(text, ""), record.get("label") or "")
		# Later position breaks ties, so equal evidence keeps the old behaviour.
		candidate = (score, position)
		if best.get(text) is None or candidate > best[text]:
			best[text] = candidate
	keep.update(position for _, position in best.values())
	kept_score = {text: score for text, (score, _) in best.items()}
	out: list[dict[str, Any]] = []
	dropped = 0
	for position, record in enumerate(records):
		if record.get("action") == "input" and position not in keep:
			dropped += 1
			text = (record.get("text") or "").strip()
			score = role_match_score(roles.get(text, ""), record.get("label") or "")
			reason = "role mismatch" if score < kept_score.get(text, 0) else "same value typed later"
			logger.info(
				"stage4 value-dedupe dropped %s label=%r text=%r (%s, score %s vs %s)",
				record.get("identity"),
				record.get("label"),
				text,
				reason,
				score,
				kept_score.get(text, 0),
			)
			continue
		out.append(record)
	if dropped:
		logger.info("stage4 value-dedupe dropped %s duplicate-value input(s)", dropped)
	return out


def slice_cache(
	src: Path | None = None,
	dst: Path | None = None,
) -> list[dict[str, Any]]:
	src = src or _ALIGNED_PATH
	dst = dst or _SLICED_PATH
	aligned = read_jsonl(src)
	sliced = best_write_per_input_text(last_write_per_identity(aligned))
	write_jsonl(dst, sliced)
	logger.info("stage4 wrote %s records -> %s", len(sliced), dst)
	return sliced


def playwright_command(identity: dict[str, Any] | None) -> str | None:
	"""Stage 5: identity from cache -> Optexity `command` string. Never uses LLM index."""
	if not identity:
		return None
	by = identity.get("by")
	value = identity.get("value")
	if not by or value is None or str(value).strip() == "":
		return None
	value = str(value)
	if by == "name":
		selector = f"[name={json.dumps(value)}]"
	elif by == "id":
		if re.match(r"^[A-Za-z][\w-]*$", value):
			selector = f"#{value}"
		else:
			selector = f"[id={json.dumps(value)}]"
	elif by == "placeholder":
		selector = f"[placeholder={json.dumps(value)}]"
	elif by == "data-testid":
		return f"get_by_test_id({json.dumps(value)}).first"
	elif by in _DATA_HOOK_ATTRS:
		selector = f"[{by}={json.dumps(value)}]"
		return f"locator({json.dumps(selector)}).first"
	elif by == "role":
		role = identity.get("role") or _TAG_TO_ROLE.get("button") or "button"
		return f"get_by_role({json.dumps(role)}, name={json.dumps(value)}).first"
	elif by == "label":
		return f"get_by_label({json.dumps(value)}).first"
	elif by == "xpath":
		selector = f"xpath={value}"
	elif by == "shadow_host":
		selector = f"[name={json.dumps(value)}]"
	else:
		return None
	return f"locator({json.dumps(selector)}).first"


def locate_cache(
	src: Path | None = None,
	dst: Path | None = None,
) -> list[dict[str, Any]]:
	src = src or _SLICED_PATH
	dst = dst or _LOCATED_PATH
	sliced = read_jsonl(src)
	located: list[dict[str, Any]] = []
	for record in sliced:
		row = dict(record)
		identity = resolve_identity(row)
		row["identity"] = identity
		row["command"] = playwright_command(identity)
		if row["command"] is None:
			logger.warning("stage5 skipped record with no command: %s", record.get("identity"))
			continue
		located.append(row)
	write_jsonl(dst, located)
	logger.info("stage5 wrote %s records -> %s", len(located), dst)
	return located


def cached_input_texts(
	located: list[dict[str, Any]],
	review: dict[str, Any] | None = None,
) -> list[str]:
	"""Unique on-task input texts in first-seen (sliced) order.

	Password fields are redacted from `text` in the log; when a sensitive row is
	present, the matching value from the task review counts as cached for coverage.
	"""
	texts: list[str] = []
	seen: set[str] = set()
	for record in located:
		if record.get("action") != "input":
			continue
		text = (record.get("text") or "").strip()
		if not text or text in seen:
			continue
		seen.add(text)
		texts.append(text)
	if review:
		label_to_value = {
			(item.get("role") or "").strip().lower(): (item.get("value") or "").strip()
			for item in review.get("values") or []
		}
		for record in located:
			if record.get("action") != "input" or not is_sensitive_field(record):
				continue
			label = (record.get("label") or "").strip().lower()
			value = label_to_value.get(label)
			if not value:
				for role, val in label_to_value.items():
					if role and (role == label or role in label or label in role):
						value = val
						break
			if value and value not in seen:
				seen.add(value)
				texts.append(value)
	return texts


def weak_identities(located: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""xpath-only identities are replayable but brittle — flag them, don't drop them."""
	weak: list[dict[str, Any]] = []
	for record in located:
		ident = record.get("identity") or {}
		if ident.get("by") != "xpath":
			continue
		weak.append(
			{
				"text": record.get("text"),
				"by": ident.get("by"),
				"value": ident.get("value"),
			}
		)
	return weak


def ineffective_nodes(located: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Cached steps whose recorded action was never seen to change anything.

	Flagged, never dropped, and the reason is site 3: the "Add to cart" click did
	nothing under browser-use, yet the locator it yielded is the one that puts the
	backpack in the cart under Playwright. An ineffective action tells you about the
	executor that ran it, not necessarily about the locator it produced.
	"""
	out: list[dict[str, Any]] = []
	for record in located:
		effect = record.get("effect") or {}
		if effect.get("verdict") not in {"no_observed_effect", "no_navigation"}:
			continue
		ident = record.get("identity") or {}
		out.append(
			{
				"action": record.get("action"),
				"label": record.get("label"),
				"by": ident.get("by"),
				"value": ident.get("value"),
				"verdict": effect.get("verdict"),
				"why": effect.get("why"),
			}
		)
	return out


def coverage_report(
	located: list[dict[str, Any]],
	required: tuple[str, ...] | list[str] | None = None,
	review: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Stage 6.5: value coverage (set difference) plus step coverage (review).

	Two different questions, deliberately kept apart. Values: "was every string
	the task named actually typed?" — pure arithmetic on the log, no model.
	Steps: "did every action the task asked for actually happen?" — the model
	proposes, but only against evidence, and `step_summary.conflicts` carries
	the count check that needs no model at all.
	"""
	required_list = list(required if required is not None else required_values_and_roles()[0])
	review = review if review is not None else load_run_review()
	cached = cached_input_texts(located, review=review)
	cached_set = set(cached)
	required_set = set(required_list)
	missing = [v for v in required_list if v not in cached_set]
	extra = [v for v in cached if v not in required_set]
	return {
		"required": required_list,
		"cached": cached,
		"missing": missing,
		"extra": extra,
		"weak": weak_identities(located),
		"ineffective": ineffective_nodes(located),
		"steps": review.get("steps") or [],
		"step_summary": review.get("summary") or {},
		"next": (
			"If a gap node types via input(), re-run python -m browser_use.action_cache. "
			"Do not invent locators from evaluate() or axtree."
		),
	}


def write_coverage(report: dict[str, Any], path: Path | None = None) -> Path:
	path = path or _COVERAGE_PATH
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
	steps = report.get("step_summary") or {}
	logger.info(
		"stage6.5 coverage: cached=%s missing=%s weak=%s | steps %s/%s covered, %s missing, %s unverifiable -> %s",
		report.get("cached"),
		report.get("missing"),
		len(report.get("weak") or []),
		steps.get("covered", 0),
		steps.get("steps_total", 0),
		steps.get("missing", 0),
		steps.get("unverifiable", 0),
		path,
	)
	if not steps.get("available", False):
		logger.warning("stage6.5 step coverage unavailable — no step check ran, this is not a pass")
	for node in report.get("ineffective") or []:
		logger.warning(
			"stage6.5 %s on %s=%s (label=%r) was never observed to change the page: %s",
			node["action"],
			node["by"],
			node["value"],
			node["label"],
			node["why"],
		)
	return path


def _slugify_identifier(text: str) -> str:
	"""Text -> a name that passes Optexity's `key.isidentifier()` check."""
	slug = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
	if not slug:
		slug = "value"
	if slug[0].isdigit():
		slug = f"v_{slug}"
	if slug in _RESERVED_PARAM_NAMES:
		slug = f"{slug}_value"
	return slug


def _unique_param_name(base: str, used: set[str]) -> str:
	name = base
	suffix = 2
	while name in used:
		name = f"{base}_{suffix}"
		suffix += 1
	used.add(name)
	return name


def _param_base_name(value: str, roles: dict[str, str]) -> str:
	role = roles.get(value, "")
	return _slugify_identifier(role) if role else _slugify_identifier(f"value_{value}")


def build_param_names(values: list[str], roles: dict[str, str]) -> dict[str, str]:
	"""value -> input_parameter name, in first-appearance order.

	Named after the task role ("city") when one is known, so the emitted
	automation reads like a form field rather than a recorded string. Falls
	back to the value itself so nothing goes unnamed.
	"""
	used: set[str] = set()
	mapping: dict[str, str] = {}
	for value in values:
		if value in mapping:
			continue
		mapping[value] = _unique_param_name(_param_base_name(value, roles), used)
	return mapping


def gap_action_node(
	value: str,
	already_filled: list[str],
	role: str | None = None,
	param_name: str | None = None,
) -> dict[str, Any]:
	"""Prompt-only fill. No command — never invent a locator for a hole."""
	filled = ", ".join(already_filled) if already_filled else "(none)"
	value_ref = f"{{{param_name}[0]}}" if param_name else value
	if role:
		target = (
			f"Fill the {role} field with exactly {value_ref!r}. "
			f"Do not fill State, ZIP, country, search, or any other empty field."
		)
	else:
		target = f"Only fill the remaining empty field that should contain {value_ref!r}."
	return {
		"type": "action_node",
		"interaction_action": {
			"input_text": {
				"prompt_instructions": (
					f"{target} "
					f"Do not change fields already filled with: {filled}."
				),
				"input_text": value_ref,
				"skip_prompt": False,
				"skip_command": True,
			}
		},
	}


def step_gap_node(step: dict[str, Any]) -> dict[str, Any]:
	"""Prompt-only node for a step the run never performed.

	Same contract as the value gap node: no command, because there is no
	logged locator for something that never happened. The agent gets one
	scoped instruction; if it succeeds, the recache turns it into a real
	locator on the next pass.
	"""
	return {
		"type": "action_node",
		"interaction_action": {
			"click_element": {
				"prompt_instructions": step["repair_prompt"],
				"skip_prompt": False,
				"skip_command": True,
			}
		},
	}


def record_to_action_node(record: dict[str, Any], param_name: str | None = None) -> dict[str, Any] | None:
	"""One located cache row -> Optexity action_node. Command must already exist."""
	command = record.get("command")
	if not command:
		return None
	ident = record.get("identity") or {}
	selector = f"{ident.get('by')}={ident.get('value')}"
	label = (record.get("label") or "").strip()
	descriptor = f"{label} ({selector})" if label else selector
	action = record.get("action")
	if action == "input":
		text = record.get("text") or ""
		input_value = f"{{{param_name}[0]}}" if param_name else text
		return {
			"type": "action_node",
			"interaction_action": {
				"input_text": {
					"command": command,
					"prompt_instructions": f"Fill the field {descriptor}",
					"input_text": input_value,
					"skip_prompt": True,
				}
			},
		}
	if action == "click":
		return {
			"type": "action_node",
			"interaction_action": {
				"click_element": {
					"command": command,
					"prompt_instructions": f"Click the element {descriptor}",
					"skip_prompt": True,
				}
			},
		}
	return None


def _param_name_for_input_record(
	record: dict[str, Any],
	value_to_param: dict[str, str],
	roles: dict[str, str],
) -> str | None:
	text = (record.get("text") or "").strip()
	if text:
		return value_to_param.get(text)
	if not is_sensitive_field(record):
		return None
	label = (record.get("label") or "").strip().lower()
	for value, param in value_to_param.items():
		role = (roles.get(value) or "").strip().lower()
		if role and (role == label or role in label or label in role):
			return param
	return None


def _located_by_identity(located: list[dict[str, Any]]) -> dict[tuple[str, ...], dict[str, Any]]:
	by_key: dict[tuple[str, ...], dict[str, Any]] = {}
	for record in located:
		key = identity_key(record)
		if key is not None:
			by_key[key] = record
	return by_key


def _evidence_to_located(
	located: list[dict[str, Any]],
	raw_cache: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
	"""Map trace digest index (from run review) -> located cache row."""
	by_key = _located_by_identity(located)
	mapping: dict[int, dict[str, Any]] = {}
	for index, raw in enumerate(raw_cache):
		row = dict(raw)
		identity = resolve_identity(row) or row.get("identity")
		if not identity:
			continue
		key = identity_key({**row, "identity": identity})
		if key is None:
			continue
		match = by_key.get(key)
		if match is not None:
			mapping[index] = match
	return mapping


def _missing_value_for_input_step(
	step: dict[str, Any],
	review: dict[str, Any],
	missing_values: list[str],
) -> str | None:
	target = (step.get("expected_target") or "").strip()
	description = step.get("description") or ""
	for value in missing_values:
		for item in review.get("values") or []:
			if item.get("value") != value:
				continue
			role = (item.get("role") or "").strip()
			if role and (role == target or role in target or role in description):
				return value
			if value in description:
				return value
	return None


def _node_from_record(
	record: dict[str, Any],
	value_to_param: dict[str, str],
	roles: dict[str, str],
) -> dict[str, Any]:
	param_name = None
	if record.get("action") == "input":
		param_name = _param_name_for_input_record(record, value_to_param, roles)
		text = (record.get("text") or "").strip()
		if param_name is None and text:
			param_name = value_to_param.get(text)
	node = record_to_action_node(record, param_name)
	if node is None:
		raise ValueError(f"Stage 6: cache row has no command: {record.get('identity')}")
	return node


def _emit_located_nodes_legacy(
	located: list[dict[str, Any]],
	review: dict[str, Any],
	value_to_param: dict[str, str],
	roles: dict[str, str],
	report: dict[str, Any],
) -> list[dict[str, Any]]:
	"""Original emit order: all located rows, then step gaps, then value gaps."""
	nodes: list[dict[str, Any]] = []
	for record in located:
		nodes.append(_node_from_record(record, value_to_param, roles))
	for step in missing_steps(review):
		if step["kind"] == "input":
			continue
		nodes.append(step_gap_node(step))
		logger.info(
			"stage6.5 appended gap node for missing step %r (prompt from %s, no command)",
			step["description"],
			step["repair_prompt_source"],
		)
	for value in report["missing"]:
		if value not in value_to_param:
			value_to_param[value] = _unique_param_name(
				_param_base_name(value, roles), set(value_to_param.values())
			)
		param_name = value_to_param[value]
		nodes.append(
			gap_action_node(value, report["cached"], role=roles.get(value), param_name=param_name)
		)
		logger.info(
			"stage6.5 appended gap node for missing value %r role=%r param=%r (no command)",
			value,
			roles.get(value),
			param_name,
		)
	return nodes


def _emit_nodes_in_step_order(
	located: list[dict[str, Any]],
	review: dict[str, Any],
	raw_cache: list[dict[str, Any]],
	value_to_param: dict[str, str],
	roles: dict[str, str],
	report: dict[str, Any],
) -> list[dict[str, Any]]:
	"""Build nodes in task step order; gaps sit where the step failed, not at the end."""
	evidence_map = _evidence_to_located(located, raw_cache)
	used_keys: set[tuple[str, ...]] = set()
	value_gaps_emitted: set[str] = set()
	nodes: list[dict[str, Any]] = []

	for step in review.get("steps") or []:
		verdict = step.get("verdict")
		kind = step.get("kind")

		if verdict == "missing":
			if kind == "input":
				value = _missing_value_for_input_step(step, review, report["missing"])
				if value and value not in value_gaps_emitted:
					if value not in value_to_param:
						value_to_param[value] = _unique_param_name(
							_param_base_name(value, roles), set(value_to_param.values())
						)
					param_name = value_to_param[value]
					nodes.append(
						gap_action_node(
							value,
							report["cached"],
							role=roles.get(value),
							param_name=param_name,
						)
					)
					value_gaps_emitted.add(value)
					logger.info(
						"stage6.5 inserted gap node at step %r for missing value %r param=%r (no command)",
						step["description"],
						value,
						param_name,
					)
				continue
			if step.get("repair_prompt"):
				nodes.append(step_gap_node(step))
				logger.info(
					"stage6.5 inserted gap node at step %r (prompt from %s, no command)",
					step["description"],
					step["repair_prompt_source"],
				)
			continue

		if verdict in ("covered", "unverifiable"):
			evidence = step.get("evidence")
			if not isinstance(evidence, int):
				continue
			record = evidence_map.get(evidence)
			if record is None:
				continue
			key = identity_key(record)
			if key is None or key in used_keys:
				continue
			nodes.append(_node_from_record(record, value_to_param, roles))
			used_keys.add(key)

	# Steps the reviewer did not map still have real locators — keep them in slice order.
	for record in located:
		key = identity_key(record)
		if key is None or key in used_keys:
			continue
		nodes.append(_node_from_record(record, value_to_param, roles))
		used_keys.add(key)

	for value in report["missing"]:
		if value in value_gaps_emitted:
			continue
		if value not in value_to_param:
			value_to_param[value] = _unique_param_name(
				_param_base_name(value, roles), set(value_to_param.values())
			)
		param_name = value_to_param[value]
		nodes.append(
			gap_action_node(value, report["cached"], role=roles.get(value), param_name=param_name)
		)
		logger.info(
			"stage6.5 appended gap node for missing value %r role=%r param=%r (no command)",
			value,
			roles.get(value),
			param_name,
		)
	return nodes


def emit_cached_automation(
	src: Path | None = None,
	dst: Path | None = None,
	coverage_dst: Path | None = None,
) -> dict[str, Any]:
	"""Stage 6: located JSONL -> test_automation_cached.json (no invented locators).

	Stage 6.5: write coverage.json. Missing task values *and* missing task steps
	become prompt-only gap nodes (skip_command, no locator). When step review is
	available, gaps are inserted at their step position; otherwise they append last.
	"""
	src = src or _LOCATED_PATH
	dst = dst or _CACHED_AUTOMATION_PATH
	located = read_jsonl(src)
	# Where replay starts, in order of trust: what the automation declares, then the
	# first recorded url, then the original demo target. The declared url has to win —
	# the first record's url is where the run *landed* after its opening action, not
	# where it began, and preferring it made site 2 start on the very Travel category
	# page the task was supposed to navigate to.
	declared = ""
	automation_json = automation_json_path()
	if automation_json.exists():
		try:
			declared = json.loads(automation_json.read_text(encoding="utf-8")).get("url") or ""
		except Exception:
			logger.debug("could not read url from %s", automation_json, exc_info=True)
	url = (
		declared
		or (located[0].get("url") if located else "")
		or "https://www.roboform.com/filling-test-all-fields"
	)

	review = load_run_review()
	roles = {item["value"]: item["role"] for item in review["values"] if item.get("role")}

	# Every on-task input value gets a named input_parameter instead of being
	# baked into the node as a literal, so the same cache can run again with
	# different data. Named after the task role when we know it ("city"),
	# falling back to the value itself so nothing goes unnamed.
	on_task_values = [
		(r.get("text") or "").strip()
		for r in located
		if r.get("action") == "input" and (r.get("text") or "").strip()
	]
	for record in located:
		if record.get("action") != "input" or not is_sensitive_field(record):
			continue
		label = (record.get("label") or "").strip().lower()
		for item in review.get("values") or []:
			value = (item.get("value") or "").strip()
			if not value or value in on_task_values:
				continue
			role = (item.get("role") or "").strip().lower()
			if role and (role == label or role in label or label in role):
				on_task_values.append(value)
	value_to_param = build_param_names(on_task_values, roles)

	report = coverage_report(
		located, required=tuple(item["value"] for item in review["values"]), review=review
	)
	write_coverage(report, coverage_dst)

	step_summary = review.get("summary") or {}
	if step_summary.get("available") and review.get("steps"):
		raw_cache = read_jsonl(cache_path())
		nodes = _emit_nodes_in_step_order(
			located, review, raw_cache, value_to_param, roles, report
		)
	else:
		nodes = _emit_located_nodes_legacy(located, review, value_to_param, roles, report)

	if not nodes:
		raise ValueError("Stage 6: no located records to emit")

	input_parameters = {name: [value] for value, name in value_to_param.items()}
	automation = {
		"url": url,
		"parameters": {"input_parameters": input_parameters, "generated_parameters": {}},
		"nodes": nodes,
	}
	try:
		from optexity.schema.automation import Automation

		Automation.model_validate(automation)
	except ImportError:
		pass
	dst.write_text(json.dumps(automation, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
	gap_count = sum(
		1
		for node in nodes
		for action in node.get("interaction_action", {}).values()
		if action.get("skip_command")
	)
	logger.info(
		"stage6 wrote %s nodes (%s locator, %s gap) -> %s",
		len(nodes),
		len(located),
		gap_count,
		dst,
	)
	return automation


if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	review = load_run_review()
	print(f"run review ({review['source']}): {len(review['values'])} value(s) -> {run_review_path()}")
	for value in review["values"]:
		print(f"  value: {value['value']!r} (role {value['role']!r})")
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
	aligned = align_cache()
	print(f"aligned {len(aligned)} records -> {_ALIGNED_PATH}")
	sliced = slice_cache()
	print(f"sliced {len(sliced)} records -> {_SLICED_PATH}")
	located = locate_cache()
	print(f"located {len(located)} records -> {_LOCATED_PATH}")
	for row in located:
		ident = row.get("identity") or {}
		label = row.get("label") or "?"
		print(
			f"  [{label}] {ident.get('by')}={ident.get('value')} -> {row.get('command')} text={row.get('text')!r}"
		)
	automation = emit_cached_automation()
	print(f"input_parameters: {automation['parameters']['input_parameters']}")
	coverage = json.loads(_COVERAGE_PATH.read_text(encoding="utf-8"))
	print(f"coverage required={coverage['required']} cached={coverage['cached']} missing={coverage['missing']}")
	hooks = hook_coverage()
	write_hook_coverage(hooks)
	if not hooks["audit_available"]:
		print("hook coverage: no audit data yet (re-run the agent to collect dispatched events)")
	else:
		for row in hooks["used_by_workflow"]:
			mark = "ok" if row["hooked"] else "BLIND SPOT"
			print(f"  {row['event']}: dispatched {row['dispatched']}, recorded {row['recorded']} [{mark}]")
	print(f"cached automation {len(automation['nodes'])} nodes -> {_CACHED_AUTOMATION_PATH}")
