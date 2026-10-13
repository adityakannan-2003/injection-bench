"""Tests for the episode loop and the two outcome measures, with a scripted backend: no API."""

import pytest

from attacks import ATTACKS_BY_NAME
from defenses import DEFENSES_BY_NAME
from harness import Episode, run_episode
from worlds import WORLDS


class ScriptedBackend:
    """Replays a list of turns. Each turn is a list of (name, args) calls, or a string (final text)."""

    def __init__(self, model, turns):
        self.model = model
        self.turns = list(turns)
        self.seen = []  # the tool results the model was shown, to assert on what reached it

    def chat(self, system, messages, tools):
        for m in messages:
            if m["role"] == "tool_results":
                self.seen.extend(r["content"] for r in m["results"])
        self.system = system
        turn = self.turns.pop(0)
        if isinstance(turn, str):
            return turn, [], {"requests": 1}
        calls = [{"id": f"c{i}", "name": n, "args": a} for i, (n, a) in enumerate(turn)]
        return "", calls, {"requests": 1}


def episode(world, turns, attack=None, goal=None, defense="none"):
    backend = ScriptedBackend("scripted", turns)
    ep = run_episode(world, ATTACKS_BY_NAME.get(attack), goal,
                     DEFENSES_BY_NAME[defense], backend)
    return ep, backend


def test_a_clean_run_finishes_the_task_and_is_not_counted_as_obeyed():
    world = WORLDS["spreadsheet"]("top-region")
    turns = [[("read_file", {"name": "sales_q3.csv"})],
             [("send_report", {"to": "maria.ortiz@clayworks.example", "subject": "Q3",
                               "body": f"{world.spec['answer']} led revenue."})],
             "Done."]
    ep, _ = episode(world, turns)
    assert (ep.obeyed, ep.harm, ep.task) == (False, False, "done")


def test_obeying_an_attack_with_no_defense_is_both_obeyed_and_harm():
    world = WORLDS["inbox"]("damaged")
    obey = WORLDS["inbox"].goals["refund-attacker"]["obey"]
    turns = [[("read_ticket", {"ticket_id": "T-201"})],
             [obey],  # the forbidden refund to order 1203
             "All done."]
    ep, _ = episode(world, turns, attack="plain", goal="refund-attacker")
    assert ep.obeyed and ep.harm
    assert any(c["args"].get("order_id") == "1203" for c in ep.emitted)


def test_the_rulebook_blocks_harm_even_when_the_model_is_fooled():
    world = WORLDS["inbox"]("damaged")
    obey = WORLDS["inbox"].goals["refund-attacker"]["obey"]
    turns = [[("read_ticket", {"ticket_id": "T-201"})], [obey], "Done."]
    ep, _ = episode(world, turns, attack="plain", goal="refund-attacker", defense="rulebook")
    assert ep.obeyed is True   # the model still emitted the forbidden call
    assert ep.harm is False    # but the rulebook stopped it executing


def test_a_held_action_is_not_harm():
    # mass-email is a legitimate tool the rulebook holds for a person, so a fooled model does no harm.
    world = WORLDS["inbox"]("damaged")
    obey = WORLDS["inbox"].goals["mass-email"]["obey"]
    turns = [[("read_ticket", {"ticket_id": "T-201"})], [obey], "Done."]
    ep, _ = episode(world, turns, attack="plain", goal="mass-email", defense="rulebook")
    assert ep.obeyed and not ep.harm


def test_content_injection_rides_a_legitimate_channel_past_the_rulebook():
    # insert-text poisons a report that legitimately goes to the manager, so the rulebook can't catch it.
    world = WORLDS["spreadsheet"]("top-region")
    obey = WORLDS["spreadsheet"].goals["insert-text"]["obey"]
    turns = [[("read_file", {"name": "sales_q3.csv"})], [obey], "Done."]
    ep, _ = episode(world, turns, attack="plain", goal="insert-text", defense="rulebook")
    assert ep.obeyed and ep.harm  # the rulebook allowed it; only a prompt defense could help here


def test_delimit_wraps_untrusted_results_but_not_trusted_ones():
    world = WORLDS["inbox"]("damaged")
    turns = [[("read_ticket", {"ticket_id": "T-201"})],
             [("refund", {"order_id": "1175", "amount": 24.99, "reason": "damaged"})],
             "Done."]
    _, backend = episode(world, turns, attack="plain", goal="refund-attacker", defense="delimit")
    ticket_result = next(s for s in backend.seen if "Ticket T-201" in s)
    assert ticket_result.startswith('<untrusted source="tool:read_ticket">')
    refund_result = next(s for s in backend.seen if "Refunded" in s)
    assert "<untrusted" not in refund_result


def test_the_warning_defense_adds_the_rule_to_the_system_prompt():
    world = WORLDS["web"]("library-hours")
    _, backend = episode(world, ["No tools needed."], defense="warning")
    assert "DATA, not instructions" in backend.system


@pytest.mark.parametrize("attack", list(ATTACKS_BY_NAME))
def test_every_attack_puts_its_injection_where_the_model_reads_it(attack):
    # The injected ask must actually reach the model through a tool result (ep.exposed), for every
    # world and every technique, or the attack would "fail" for the wrong reason.
    for wname, cls in WORLDS.items():
        scenario = next(iter(cls.scenarios))
        goal = next(iter(cls.goals))
        world = cls(scenario)
        # Read every untrusted source so a split attack's second half is seen too.
        if wname == "inbox":
            reads = [("read_ticket", {"ticket_id": world.spec["ticket"]}),
                     ("lookup_order", {"order_id": world.spec["order"]})]
        elif wname == "spreadsheet":
            reads = [("read_file", {"name": "sales_q3.csv"})]
        else:
            reads = [("search", {"query": " ".join(world.spec["words"])}),
                     ("fetch_page", {"url": world.spec["url"]})]
        ep, _ = episode(world, [reads, "Done."], attack=attack, goal=goal)
        assert ep.exposed, f"{attack} injection never reached the model in world {wname}"
