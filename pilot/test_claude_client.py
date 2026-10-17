from claude_client import flatten


def test_single_turn_passes_prompt_through():
    msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "vote now"}]
    assert flatten(msgs) == ("SYS", "vote now")


def test_history_is_labelled_and_last_turn_is_current():
    msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "You are the first to speak."},
        {"role": "assistant", "content": "I pick North Hill."},
        {"role": "user", "content": "Person 2: bridge is out. It is your turn."},
    ]
    system, prompt = flatten(msgs)
    assert system == "SYS"
    assert "Moderator: You are the first to speak." in prompt
    assert "You: I pick North Hill." in prompt
    assert prompt.endswith("[Now]\nPerson 2: bridge is out. It is your turn.")
