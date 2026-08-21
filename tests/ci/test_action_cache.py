"""Unit tests for the action cache filter pipeline.

The cache was verified end to end against four live sites, which proves the happy path
and almost nothing else. Every bug this project actually hit was an *edge* in a filter:
a requirement list that came back empty and deleted every typed value, a url change
credited to a keystroke, a stale baseline that called a dead click a success. Live runs
cannot pin those down, because reproducing them means getting a real website into a
particular state.

So these tests are deliberately not "does the pipeline work". They are one test per
*rule*, stated as the rule, on hand-built records — no browser, no network, no LLM.

ON THE WORD "FIXED" BELOW
-------------------------
Several docstrings describe a bug and are marked FIXED. Those bugs are **not open**.
Each one was found, diagnosed and corrected before these tests were written, and the
test exists so it cannot come back: a fixed bug with no test is one well-meaning
"simplification" away from returning, and the next person to read the code has no way
of knowing why the guard is there. Naming the bug in the test is what turns a fix into
a permanent one.

Every FIXED marker here was verified by putting the old bug back and confirming this
suite fails, so these are guards that demonstrably bite — not decoration.
"""

import pytest

from browser_use.action_cache import (
	action_effect,
	annotate_effects,
	best_write_per_input_text,
	effect_rank,
	identity_key,
	ineffective_nodes,
	is_on_task,
	last_write_per_identity,
	playwright_command,
	record_to_action_node,
	role_match_score,
	stable_identity,
	task_values_from_text,
	weak_identities,
)

pytestmark = pytest.mark.unit


def record(**overrides):
	"""A minimally valid cache record. Tests override only what they are about."""
	base = {
		"action": "click",
		"index": 7,
		"text": None,
		"url": "https://example.com/a",
		"identity": {"by": "name", "value": "field"},
		"label": "",
	}
	base.update(overrides)
	return base


def snapshot(title="T", interactive=10, text_len=100, target=None):
	"""What the effect probe returns: three page numbers plus the touched element."""
	return {"title": title, "interactive": interactive, "text_len": text_len, "target": target}


class TestStableIdentity:
	"""Stage 2. browser-use addresses elements as `[13]`, which is meaningless on the
	next render. Nothing downstream may ever depend on that index."""

	def test_prefers_name_over_id_and_placeholder(self):
		ident = stable_identity({"name": "user", "id": "u1", "placeholder": "Username"})
		assert ident == {"by": "name", "value": "user"}

	def test_falls_back_through_the_attribute_order(self):
		assert stable_identity({"id": "u1", "placeholder": "P"})["by"] == "id"
		assert stable_identity({"placeholder": "P"})["by"] == "placeholder"

	def test_ignores_blank_attributes(self):
		"""An attribute present but empty is not a handle."""
		assert stable_identity({"name": "   ", "id": "real"}) == {"by": "id", "value": "real"}

	def test_xpath_is_the_last_resort_not_the_first(self):
		ident = stable_identity({"name": "user"}, xpath="html/body/div[3]/input")
		assert ident["by"] == "name", "a positional xpath must never beat a stable attribute"

	def test_returns_none_when_there_is_no_handle_at_all(self):
		"""No handle must mean "we have nothing", never a guess."""
		assert stable_identity({}, xpath="") is None

	def test_never_returns_the_llm_index(self):
		ident = stable_identity({"name": "user", "data-index": "13"})
		assert "index" not in str(ident)


class TestPlaywrightCommand:
	"""Stage 5. Identity -> Optexity command string."""

	@pytest.mark.parametrize(
		"identity,expected",
		[
			({"by": "name", "value": "user-name"}, 'locator("[name=\\"user-name\\"]").first'),
			({"by": "id", "value": "search-query"}, 'locator("#search-query").first'),
			({"by": "placeholder", "value": "Search"}, 'locator("[placeholder=\\"Search\\"]").first'),
			({"by": "xpath", "value": "html/body/a"}, 'locator("xpath=html/body/a").first'),
		],
	)
	def test_builds_the_expected_selector(self, identity, expected):
		assert playwright_command(identity) == expected

	def test_an_id_that_is_not_a_bare_word_uses_attribute_form(self):
		"""`#foo:bar` would parse as a CSS pseudo-selector, so quote it instead."""
		assert playwright_command({"by": "id", "value": "foo:bar"}) == 'locator("[id=\\"foo:bar\\"]").first'

	@pytest.mark.parametrize("identity", [None, {}, {"by": "name", "value": ""}, {"by": "wat", "value": "x"}])
	def test_returns_none_rather_than_inventing_a_selector(self, identity):
		"""Stage 5 has exactly one failure mode, and it is admitting it has nothing."""
		assert playwright_command(identity) is None


class TestStage3OnTaskFilter:
	"""Stage 3 keeps clicks and keeps only typed values the task asked for."""

	def test_clicks_are_always_kept(self):
		assert is_on_task(record(action="click"), ("myname",)) is True

	def test_input_of_a_required_value_is_kept(self):
		assert is_on_task(record(action="input", text="myname"), ("myname",)) is True

	def test_input_of_something_nobody_asked_for_is_dropped(self):
		assert is_on_task(record(action="input", text="junk"), ("myname",)) is False

	def test_empty_requirement_list_keeps_everything(self):
		"""FIXED, guarded here. An empty list means "extraction found nothing", not "nothing is
		allowed". Reading it the second way deleted every typed value on any task not
		phrased "role as value" — a silent, confident-looking deletion."""
		assert is_on_task(record(action="input", text="anything"), ()) is True

	def test_input_with_no_text_is_dropped(self):
		assert is_on_task(record(action="input", text=""), ("myname",)) is False


class TestTaskValueExtraction:
	def test_pulls_values_out_of_role_as_value_phrasing(self):
		values = task_values_from_text("fill the full name as myname, address line one as xyz, city as SF")
		assert values == ("myname", "xyz", "SF")

	def test_returns_nothing_when_it_matches_nothing(self):
		"""FIXED, guarded here. This used to substitute Roboform's values when the regex missed,
		so a filter that understood nothing looked like a filter that worked."""
		assert task_values_from_text("log in and put the backpack in the cart") == ()

	def test_tolerates_empty_input(self):
		assert task_values_from_text("") == ()


class TestStage4IdentityDedupe:
	"""One record per element, so a retried field does not become two nodes."""

	def test_collapses_repeats_of_the_same_element(self):
		rows = [
			record(action="input", text="first", identity={"by": "name", "value": "f"}),
			record(action="input", text="second", identity={"by": "name", "value": "f"}),
		]
		out = last_write_per_identity(rows)
		assert len(out) == 1 and out[0]["text"] == "second"

	def test_keeps_distinct_elements_in_first_seen_order(self):
		rows = [
			record(identity={"by": "name", "value": "a"}),
			record(identity={"by": "name", "value": "b"}),
			record(identity={"by": "name", "value": "a"}),
		]
		assert [r["identity"]["value"] for r in last_write_per_identity(rows)] == ["a", "b"]

	def test_prefers_the_attempt_that_demonstrably_did_something(self):
		"""The whole point of effect tracking. Ten clicks on one button, one of which
		worked: the survivor must be the one that worked, not the one that came last."""
		rows = [
			record(text="worked", effect={"verdict": "effective"}),
			record(text="did nothing", effect={"verdict": "no_observed_effect"}),
		]
		assert last_write_per_identity(rows)[0]["text"] == "worked"

	def test_falls_back_to_last_write_on_equal_evidence(self):
		"""A trace with no effect data must behave exactly as it did before this rule."""
		rows = [record(text="first"), record(text="second")]
		assert last_write_per_identity(rows)[0]["text"] == "second"

	def test_records_with_no_identity_are_skipped_not_kept(self):
		assert last_write_per_identity([record(identity=None)]) == []
		assert identity_key(record(identity={"by": "name", "value": ""})) is None

	def test_effect_rank_never_punishes_ignorance_as_hard_as_failure(self):
		"""`unknown` means "we could not measure it", which must outrank "we measured
		it and it did nothing" — otherwise legacy records lose to failures."""
		assert effect_rank({"effect": {"verdict": "effective"}}) > effect_rank({"effect": {"verdict": "unknown"}})
		assert effect_rank({"effect": {"verdict": "unknown"}}) > effect_rank({"effect": {"verdict": "no_observed_effect"}})
		assert effect_rank({}) == effect_rank({"effect": {"verdict": "unknown"}}), "missing data ranks neutral"


class TestRoleAwareValueDedupe:
	"""The "SF" case, and the reason label capture exists.

	"SF" is a valid city *and* a valid state, so browser-use typed it into both boxes.
	The original rule was "keep whichever was typed last", which happened to be City
	and happened to be right. That is luck, not logic. Matching the field's on-page
	label against the task's role is the actual rule."""

	CITY = {"by": "name", "value": "13adr_city"}
	STATE = {"by": "name", "value": "12adr_state"}

	def test_keeps_the_field_whose_label_matches_the_task_role(self):
		rows = [
			record(action="input", text="SF", identity=self.STATE, label="State"),
			record(action="input", text="SF", identity=self.CITY, label="City"),
		]
		kept = best_write_per_input_text(rows, roles={"SF": "city"})
		assert len(kept) == 1
		assert kept[0]["label"] == "City"

	def test_and_does_so_when_the_wrong_field_was_typed_last(self):
		"""The test that proves it is no longer order-dependent. This is the exact case
		the old last-write-wins rule got wrong; it only ever passed by accident."""
		rows = [
			record(action="input", text="SF", identity=self.CITY, label="City"),
			record(action="input", text="SF", identity=self.STATE, label="State"),
		]
		kept = best_write_per_input_text(rows, roles={"SF": "city"})
		assert len(kept) == 1
		assert kept[0]["label"] == "City", "later position must not beat a better label match"

	def test_falls_back_to_last_write_when_no_labels_were_captured(self):
		"""Older traces have no labels. They must still compile, not crash or empty out."""
		rows = [
			record(action="input", text="SF", identity=self.CITY, label=""),
			record(action="input", text="SF", identity=self.STATE, label=""),
		]
		kept = best_write_per_input_text(rows, roles={"SF": "city"})
		assert len(kept) == 1 and kept[0]["identity"] == self.STATE

	def test_different_values_are_never_in_competition(self):
		rows = [
			record(action="input", text="myname", identity={"by": "name", "value": "n"}, label="Full Name"),
			record(action="input", text="SF", identity=self.CITY, label="City"),
		]
		assert len(best_write_per_input_text(rows, roles={})) == 2

	def test_clicks_are_untouched_by_value_dedupe(self):
		rows = [record(action="click"), record(action="click")]
		assert len(best_write_per_input_text(rows, roles={})) == 2

	@pytest.mark.parametrize(
		"role,label,expected",
		[("city", "City", 3), ("city", "Billing City", 2), ("address line one", "Address Line 1", 1), ("city", "State", 0), ("city", "", 0)],
	)
	def test_role_match_scoring(self, role, label, expected):
		assert role_match_score(role, label) == expected


class TestActionEffect:
	"""Executed versus achieved. browser-use reports a click as successful whenever
	the click was *dispatched*, which is why ten logged add-to-cart successes sat next
	to an empty cart. This asks a different question: did anything change?"""

	def test_a_click_that_navigated_is_effective(self):
		before = record(url="https://x/a", after=snapshot())
		after = record(action="click", url="https://x/b", after=snapshot(title="B"))
		assert action_effect(after, before)["verdict"] == "effective"

	def test_typing_is_verified_by_reading_the_field_back(self):
		before = record(url="https://x/a", after=snapshot())
		typed = record(
			action="input", text="standard_user", url="https://x/a",
			after=snapshot(target={"present": True, "value": "standard_user"}),
		)
		effect = action_effect(typed, before)
		assert effect["verdict"] == "effective"
		assert effect["typed_value_present"] is True

	def test_typing_the_wrong_thing_is_not_effective(self):
		before = record(url="https://x/a", after=snapshot())
		typed = record(
			action="input", text="standard_user", url="https://x/a",
			after=snapshot(target={"present": True, "value": "secret_sauce"}),
		)
		assert action_effect(typed, before)["verdict"] != "effective"

	def test_typing_is_never_credited_with_a_navigation(self):
		"""FIXED, guarded here. A url change straddling a keystroke came from something
		else — in the trace that caught this, a new pass restarting at the login page.
		Crediting it here manufactured evidence for a step nobody performed."""
		before = record(url="https://x/inventory")
		typed = record(action="input", text="standard_user", url="https://x/login")
		assert action_effect(typed, before)["verdict"] == "unknown"

	def test_a_click_that_changed_nothing_at_all_is_reported_as_such(self):
		unchanged = snapshot(target={"present": True, "value": None})
		before = record(url="https://x/a", after=unchanged)
		dead = record(action="click", url="https://x/a", after=unchanged)
		assert action_effect(dead, before)["verdict"] == "no_observed_effect"

	def test_a_text_length_wobble_alone_proves_nothing(self):
		"""FIXED, guarded here. Body text length drifts on re-render. Treating that as
		proof of effect declared a dead "Add to cart" click a success."""
		before = record(url="https://x/a", after=snapshot(text_len=1435))
		click = record(action="click", url="https://x/a", after=snapshot(text_len=1385, target={"present": True}))
		assert action_effect(click, before)["verdict"] == "unknown"

	def test_a_reading_taken_across_a_navigation_is_not_a_baseline(self):
		"""FIXED, guarded here. The first reading after a navigation catches the new page
		mid-render, so diffing against it measures the render, not the action."""
		before = record(action="click", url="https://x/inventory", after=snapshot(interactive=20))
		click = record(action="click", url="https://x/inventory", after=snapshot(interactive=25, target={"present": True}))
		effect = action_effect(click, before, previous_effect={"url_changed": True})
		assert effect["verdict"] != "effective", "a stale baseline must not manufacture a success"

	def test_a_click_whose_target_vanished_is_effective(self):
		""""Add to cart" becoming "Remove" is the element being replaced."""
		before = record(url="https://x/a", after=snapshot())
		click = record(action="click", url="https://x/a", after=snapshot(target={"present": False}))
		assert action_effect(click, before)["verdict"] == "effective"

	def test_a_record_with_no_probe_data_is_unknown_not_a_failure(self):
		"""Traces predate the probe. Reading absent data as absence of effect would
		condemn every historical run we still compile from."""
		legacy = record(action="input", text="x")
		assert action_effect(legacy, None)["verdict"] == "unknown"

	def test_a_click_that_did_not_navigate_is_weak_evidence_not_proof(self):
		before = record(url="https://x/a")
		click = record(action="click", url="https://x/a")
		assert action_effect(click, before)["verdict"] == "no_navigation"


class TestAnnotateEffects:
	def test_every_record_gets_a_verdict(self):
		rows = [record(url="https://x/a"), record(url="https://x/b"), record(url="https://x/b")]
		assert all("effect" in r for r in annotate_effects(rows))

	def test_the_first_record_has_nothing_to_compare_against(self):
		assert annotate_effects([record()])[0]["effect"]["verdict"] == "unknown"

	def test_input_does_not_mutate_the_caller_records(self):
		rows = [record()]
		annotate_effects(rows)
		assert "effect" not in rows[0]


class TestCoverageFlags:
	"""Both of these flag and never drop. A brittle locator still replays, and an
	action that did nothing under browser-use may still yield a locator that works
	under Playwright — as saucedemo's add-to-cart did."""

	def test_xpath_only_identities_are_flagged_as_weak(self):
		located = [
			record(identity={"by": "xpath", "value": "html/body/div[3]/a[5]"}),
			record(identity={"by": "name", "value": "user"}),
		]
		weak = weak_identities(located)
		assert len(weak) == 1 and weak[0]["by"] == "xpath"

	def test_actions_with_no_observed_effect_are_flagged(self):
		located = [
			record(effect={"verdict": "no_observed_effect", "why": "the page did not change at all"}),
			record(effect={"verdict": "effective", "why": "the page navigated"}),
		]
		assert len(ineffective_nodes(located)) == 1

	def test_unknown_is_not_reported_as_ineffective(self):
		""""We could not measure it" is not "it did nothing"."""
		assert ineffective_nodes([record(effect={"verdict": "unknown"})]) == []


class TestEmittedNode:
	"""Stage 6. What actually lands in test_automation_cached.json."""

	def test_an_input_node_carries_the_command_and_skips_the_prompt(self):
		node = record_to_action_node(
			record(action="input", text="myname", command='locator("[name=\\"n\\"]").first', label="Full Name")
		)
		action = node["interaction_action"]["input_text"]
		assert action["command"] == 'locator("[name=\\"n\\"]").first'
		assert action["skip_prompt"] is True, "a cached node must not call the LLM"
		assert action["input_text"] == "myname"

	def test_a_parameterized_node_references_the_parameter(self):
		node = record_to_action_node(
			record(action="input", text="myname", command="locator(x).first"), param_name="full_name"
		)
		assert node["interaction_action"]["input_text"]["input_text"] == "{full_name[0]}"

	def test_the_captured_label_reaches_the_instructions(self):
		"""What makes an emitted node readable: "Full Name", not just "name=04fullname"."""
		node = record_to_action_node(
			record(action="input", text="x", command="locator(x).first", label="Full Name")
		)
		assert "Full Name" in node["interaction_action"]["input_text"]["prompt_instructions"]

	def test_a_record_with_no_command_emits_no_node(self):
		"""The no-invented-locators rule, at the last place it could be broken."""
		assert record_to_action_node(record(command=None)) is None
