"""The transcript can only grow, and only in shapes the API accepts."""

from __future__ import annotations

import pytest

from msa.llm import fake
from msa.llm.transcript import Transcript, TranscriptError


def with_calls(*ids: str) -> Transcript:
    transcript = Transcript("go")
    transcript.add_assistant([fake.tool_use(i, "read_file", {"path": i}) for i in ids])
    return transcript


def result(call_id: str) -> dict[str, str]:
    return {"type": "tool_result", "tool_use_id": call_id, "content": "ok"}


def test_it_starts_with_the_prompt_as_a_user_message() -> None:
    transcript = Transcript("review this")
    assert transcript.messages == [{"role": "user", "content": "review this"}]
    assert (len(transcript), transcript.last_role) == (1, "user")


def test_reading_the_messages_never_exposes_the_history_to_mutation() -> None:
    blocks = [fake.text("hi")]
    transcript = Transcript(blocks)
    blocks[0]["text"] = "changed by the caller after the fact"
    transcript.messages[0]["content"][0]["text"] = "changed through the view"
    assert transcript.messages[0]["content"] == [fake.text("hi")]


def test_appended_content_is_copied_on_the_way_in() -> None:
    transcript = Transcript("go")
    content = [fake.text("answer")]
    transcript.add_assistant(content)
    content.append(fake.text("smuggled"))
    assert transcript.messages[-1] == {"role": "assistant", "content": [fake.text("answer")]}


def test_an_empty_assistant_message_is_refused() -> None:
    with pytest.raises(TranscriptError, match="needs content"):
        Transcript("go").add_assistant([])


def test_consecutive_assistant_messages_continue_a_paused_turn() -> None:
    transcript = Transcript("go")
    transcript.add_assistant([fake.text("part one")])
    transcript.add_assistant([fake.text("part two")])
    assert [m["role"] for m in transcript.messages] == ["user", "assistant", "assistant"]


def test_tool_results_answer_every_call_in_any_order() -> None:
    transcript = with_calls("a", "b")
    transcript.add_tool_results([result("b"), result("a")])
    assert transcript.messages[-1] == {"role": "user", "content": [result("b"), result("a")]}


@pytest.mark.parametrize(
    "results",
    [[result("a")], [result("a"), result("b"), result("c")], [result("a"), result("a")], []],
    ids=["missing", "extra", "duplicate", "none"],
)
def test_tool_results_must_answer_exactly_the_calls_made(results: list[dict[str, str]]) -> None:
    transcript = with_calls("a", "b")
    with pytest.raises(TranscriptError, match=r"do not answer calls \['a', 'b'\]"):
        transcript.add_tool_results(results)
    assert len(transcript) == 2


def test_tool_results_must_follow_an_assistant_message() -> None:
    with pytest.raises(TranscriptError, match="must answer an assistant message"):
        Transcript("go").add_tool_results([result("a")])


def test_operator_instructions_are_appended_after_a_user_message() -> None:
    transcript = with_calls("a")
    transcript.add_tool_results([result("a")])
    transcript.add_system("stop editing tests")
    assert transcript.messages[-1] == {"role": "system", "content": "stop editing tests"}


def test_operator_instructions_cannot_interrupt_an_assistant_turn() -> None:
    with pytest.raises(TranscriptError, match="must follow a user message"):
        with_calls("a").add_system("too late")


def test_a_later_conversation_extends_this_one_only_if_its_prefix_is_identical() -> None:
    transcript = with_calls("a")
    later = with_calls("a")
    later.add_tool_results([result("a")])
    assert transcript.is_extended_by(later.messages)
    assert transcript.is_extended_by(transcript.messages)
    assert not transcript.is_extended_by(transcript.messages[:1])
    edited = later.messages
    edited[1]["content"][0]["input"] = {"path": "other"}
    assert not transcript.is_extended_by(edited)
