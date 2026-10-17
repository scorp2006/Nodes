"""Offline checks for the evidence board. Needs FalkorDB on localhost:6379, no LLM."""
import random
from pathlib import Path

from hiddenbench import load_benchmark
from hiddenbench.prompts import load_prompts
from hiddenbench.simulator import instantiate_agents, run_scenario

import graph_tools

TASKS = Path(__file__).resolve().parent.parent / "external" / "HiddenBench_ICML" / "data" / "benchmark_short.json"


class FakeClient:
    model_name = "fake"

    def __init__(self, answer):
        self.answer, self.systems = answer, set()

    def chat(self, messages):
        self.systems.add(messages[0]["content"])
        return "I agree."

    def chat_json(self, messages, schema):
        self.systems.add(messages[0]["content"])
        return {"vote": self.answer, "rationale": "fake"}


def setup(graph="test_board"):
    task = load_benchmark(path=str(TASKS))[1]
    agents, assignments = instantiate_agents(task, FakeClient(task.correct_answer), "hidden", load_prompts(),
                                             random.Random(7), extra_prompt="x", special_agent_ratio=1.0)
    g = graph_tools.connect(graph)
    try:
        g.delete()
    except Exception:
        pass
    graph_tools.seed(graph, task, assignments)
    return task, agents, assignments, g


def test_board_flow():
    task, agents, assignments, g = setup()
    assert g.ro_query("MATCH (a:Agent) RETURN count(a)").result_set[0][0] == len(task.hidden_information)
    assert "empty" in graph_tools.shared_facts(g)
    assert set(graph_tools.who_has_not_shared(g).split(": ")[1].split(", ")) == {a["agent"] for a in assignments}

    # Person 1 posts their private fact, by the number they see in their own briefing.
    p1 = assignments[0]
    hidden = p1["assigned_hidden_information"][0]
    num = p1["visible_facts"].index(hidden) + 1
    assert hidden in graph_tools.share_fact(g, p1["agent"], num)
    assert "no Fact 99" in graph_tools.share_fact(g, p1["agent"], 99)

    board = graph_tools.shared_facts(g)
    assert hidden in board and p1["agent"] in board
    # no other private fact leaks onto the board
    for other in assignments[1:]:
        assert other["assigned_hidden_information"][0] not in board
    assert p1["agent"] not in graph_tools.who_has_not_shared(g)

    # sharing twice does not duplicate
    graph_tools.share_fact(g, p1["agent"], num)
    assert g.ro_query("MATCH ()-[s:SHARED]->() RETURN count(s)").result_set[0][0] == 1
    g.delete()


def test_notepad_is_private_and_adds_no_information():
    task, agents, assignments, g = setup("test_notepad")
    a, b = assignments[0], assignments[1]
    graph_tools.write_note(g, a["agent"], "secret plan")
    assert "secret plan" in graph_tools.read_notes(g, a["agent"])
    assert "no notes" in graph_tools.read_notes(g, b["agent"])
    mine = graph_tools.my_facts(g, a["agent"])
    assert all(fact in mine for fact in a["visible_facts"])
    assert b["assigned_hidden_information"][0] not in mine
    g.delete()


def test_runner_rebuilds_the_same_agents():
    """The runner pre-builds agents to wire tools; run_scenario must build identical ones."""
    task = load_benchmark(path=str(TASKS))[1]
    prompts = load_prompts()
    pre, _ = instantiate_agents(task, None, "hidden", prompts, random.Random(11),
                                extra_prompt="x", special_agent_ratio=1.0)
    fake = FakeClient(task.correct_answer)
    run_scenario(task, fake, "hidden", prompts, rounds=1, seed=11, extra_prompt="x", special_agent_ratio=1.0)
    assert fake.systems == {a.history[0]["content"] for a in pre}
