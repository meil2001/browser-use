"""Run 1 (agentic) for the second site — books.toscrape.com.

Mirrors how the Roboform Run 1 evidently ran (no Task ID / worker log in
RUN2_METRICS.md's Run 1 section): a plain browser_use.Agent script, not
routed through the Optexity inference server. The hook in
browser_use/tools/service.py fires regardless of who calls the action
functions, so this still populates the cache exactly like Run 2's server
path would.

Artifacts are isolated under run_logs/site2/ via ACTION_CACHE_PATH /
ACTION_DISPATCH_LOG, so this never touches the Roboform cache.
"""

import asyncio
import logging
import os
from pathlib import Path

os.environ["ACTION_CACHE_PATH"] = "run_logs/site2/action_cache.jsonl"
os.environ["ACTION_DISPATCH_LOG"] = "run_logs/site2/dispatched_events.jsonl"
os.environ["TASK_SPEC_PATH"] = "run_logs/site2/task_spec.json"

from browser_use import Agent, BrowserProfile
from optexity.inference.models.chat_litellm import build_agent_llm

logging.basicConfig(level=logging.INFO)
logging.getLogger("browser_use").setLevel(logging.INFO)

TASK = (
	'Go to the "Travel" category. On the Travel category page, click on the '
	'book titled "It\'s Only the Himalayas" to open its detail page. '
	'On the book detail page, click the "Add to basket" button. '
	"Then stop — do not add any other book."
)


async def main() -> None:
	Path("run_logs/site2").mkdir(parents=True, exist_ok=True)
	llm = build_agent_llm()
	agent = Agent(
		task=TASK,
		llm=llm,
		browser_profile=BrowserProfile(headless=True),
		max_actions_per_step=1,
	)
	history = await agent.run(max_steps=15)
	print("agent done. steps:", len(history.history))
	print("final result:", history.final_result())
	print("is_successful:", history.is_successful())
	usage = history.usage
	if usage is not None:
		print(
			f"tokens: prompt={usage.total_prompt_tokens} "
			f"completion={usage.total_completion_tokens} total={usage.total_tokens} "
			f"cost=${usage.total_cost:.4f}"
		)


if __name__ == "__main__":
	asyncio.run(main())
