"""
Tests for the LLM tool loop (agent/llm_task.py).

The point of the mock providers is that the *loop* is testable without a GPU:
iteration limits, tool dispatch, policy interruptions and transcript shape all
get exercised here against scripted turns.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

import pytest

from llm_task import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TOOL_KINDS,
    MAX_ALLOWED_ITERATIONS,
    MOCK_PROVIDERS,
    STOP_REASONS,
    EchoProvider,
    HttpProvider,
    KeywordToolProvider,
    LlmTaskError,
    LlmTaskResult,
    LlmTaskRunner,
    Provider,
    ProviderResponse,
    ScriptedProvider,
    ToolCall,
    build_provider,
)


@dataclass
class FakeResult:
    ok: bool = True
    decision: str = "auto"
    stdout: str = "output"
    stderr: str = ""
    exit_code: int | None = 0
    error: str = ""


def shell_turn(command: str = "ls", call_id: str = "call-1") -> dict[str, Any]:
    return {"tool_calls": [{"id": call_id, "name": "shell", "arguments": {"command": command}}]}


def runner(execute: Any = None, **kw: Any) -> LlmTaskRunner:
    return LlmTaskRunner(execute or (lambda task: FakeResult()), **kw)


# ---------------------------------------------------------------------------
# ToolCall
# ---------------------------------------------------------------------------
def test_openai_nested_shape_parses() -> None:
    call = ToolCall.from_dict(
        {"id": "c1", "function": {"name": "shell", "arguments": '{"command": "ls"}'}}
    )
    assert (call.id, call.name, call.arguments) == ("c1", "shell", {"command": "ls"})


def test_flat_shape_parses() -> None:
    call = ToolCall.from_dict({"name": "shell", "arguments": {"command": "ls"}}, index=2)
    assert call.id == "call-2"
    assert call.arguments == {"command": "ls"}


def test_missing_arguments_default_to_empty() -> None:
    assert ToolCall.from_dict({"name": "shell"}).arguments == {}


def test_empty_argument_string_is_an_empty_object() -> None:
    assert ToolCall.from_dict({"name": "shell", "arguments": ""}).arguments == {}


def test_unnamed_tool_call_is_rejected() -> None:
    with pytest.raises(LlmTaskError, match="no name"):
        ToolCall.from_dict({"arguments": {}})


def test_unparseable_arguments_are_rejected() -> None:
    with pytest.raises(LlmTaskError, match="unparseable"):
        ToolCall.from_dict({"name": "shell", "arguments": "{not json"})


def test_non_object_arguments_are_rejected() -> None:
    with pytest.raises(LlmTaskError, match="must be an object"):
        ToolCall.from_dict({"name": "shell", "arguments": "[1, 2]"})


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
def test_base_provider_is_abstract() -> None:
    with pytest.raises(NotImplementedError):
        Provider().complete([], [])


def test_echo_provider_returns_the_last_user_message() -> None:
    provider = EchoProvider()
    response = provider.complete([{"role": "user", "content": "hi"}], [])
    assert response.content == "echo: hi"
    assert response.wants_tools is False


def test_scripted_provider_replays_turns_in_order() -> None:
    provider = ScriptedProvider(["one", "two"])
    assert provider.complete([], []).content == "one"
    assert provider.complete([], []).content == "two"


def test_scripted_provider_stops_rather_than_raising_when_exhausted() -> None:
    """A loop that is supposed to stop should stop, not blow up."""
    provider = ScriptedProvider([], exhausted_message="finished")
    assert provider.complete([], []).content == "finished"


def test_scripted_provider_accepts_response_objects_and_dicts() -> None:
    provider = ScriptedProvider([ProviderResponse(content="a"), shell_turn()])
    assert provider.complete([], []).content == "a"
    assert provider.complete([], []).wants_tools is True


def test_scripted_provider_rejects_nonsense_turns() -> None:
    with pytest.raises(LlmTaskError, match="cannot interpret"):
        ScriptedProvider([42])


def test_scripted_provider_records_the_transcripts_it_saw() -> None:
    provider = ScriptedProvider(["done"])
    provider.complete([{"role": "user", "content": "x"}], [])
    assert provider.calls[0][0]["content"] == "x"


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("read the config", "read_file"),
        ("list the directory", "list_dir"),
        ("run the build", "shell"),
    ],
)
def test_keyword_provider_picks_a_tool_from_the_prompt(prompt: str, expected: str) -> None:
    provider = KeywordToolProvider(target="/tmp")
    response = provider.complete([{"role": "user", "content": prompt}], list(DEFAULT_TOOL_KINDS))
    assert response.tool_calls[0].name == expected


def test_keyword_provider_prefers_read_over_list() -> None:
    """Ordered rules: 'read the listing' should read, not list."""
    provider = KeywordToolProvider()
    response = provider.complete(
        [{"role": "user", "content": "read the listing"}], list(DEFAULT_TOOL_KINDS)
    )
    assert response.tool_calls[0].name == "read_file"


def test_keyword_provider_summarises_once_it_has_seen_results() -> None:
    provider = KeywordToolProvider()
    response = provider.complete(
        [{"role": "user", "content": "run it"}, {"role": "tool", "content": "out"}], ["shell"]
    )
    assert response.content == "observed 1 tool result(s)"
    assert response.wants_tools is False


def test_keyword_provider_does_nothing_without_a_match() -> None:
    provider = KeywordToolProvider()
    assert provider.complete([{"role": "user", "content": "hello"}], ["shell"]).content == "nothing to do"


def test_keyword_provider_skips_unavailable_tools() -> None:
    provider = KeywordToolProvider()
    assert provider.complete([{"role": "user", "content": "run it"}], []).content == "nothing to do"


def test_http_provider_adapts_the_wire_shape() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.seen: list[Any] = []

        def chat(self, messages: list[dict], tools: Any = None) -> dict:
            self.seen.append(tools)
            return {
                "choices": [
                    {"message": {"content": "hi", "tool_calls": [{"id": "c", "name": "shell"}]}}
                ]
            }

    client = FakeClient()
    schemas = [{"function": {"name": "shell"}}, {"function": {"name": "grep"}}]
    provider = HttpProvider(client, tool_schemas=schemas)
    response = provider.complete([{"role": "user", "content": "x"}], ["shell"])
    assert response.content == "hi"
    assert response.tool_calls[0].name == "shell"
    assert client.seen[0] == [{"function": {"name": "shell"}}]


def test_http_provider_survives_a_junk_response() -> None:
    class JunkClient:
        def chat(self, messages: list[dict], tools: Any = None) -> str:
            return "not a dict"

    assert HttpProvider(JunkClient()).complete([], []).content == ""


# ---------------------------------------------------------------------------
# build_provider
# ---------------------------------------------------------------------------
def test_default_provider_is_echo() -> None:
    assert build_provider({}).name == "echo"


def test_scripted_provider_is_addressable_by_name() -> None:
    provider = build_provider({"provider": "scripted", "script": ["done"]})
    assert isinstance(provider, ScriptedProvider)


def test_turns_is_accepted_as_an_alias_for_script() -> None:
    assert isinstance(build_provider({"provider": "scripted", "turns": ["x"]}), ScriptedProvider)


def test_scripted_provider_needs_a_list() -> None:
    with pytest.raises(LlmTaskError, match="'script' list"):
        build_provider({"provider": "scripted", "script": "done"})


def test_keyword_provider_takes_a_target() -> None:
    assert build_provider({"provider": "keyword", "target": "/etc"}).target == "/etc"


def test_unknown_provider_lists_the_alternatives() -> None:
    with pytest.raises(LlmTaskError, match="available:"):
        build_provider({"provider": "gpt-9"})


def test_http_is_not_addressable_from_a_packet() -> None:
    """It needs a live client, which a task packet cannot supply."""
    assert "http" not in MOCK_PROVIDERS


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
def test_a_final_message_ends_the_loop() -> None:
    outcome = runner().run("hi", ScriptedProvider(["all done"]))
    assert outcome.ok is True
    assert outcome.stop_reason == "final"
    assert outcome.final_message == "all done"
    assert outcome.iterations == 1


def test_tool_call_then_summary() -> None:
    seen: list[Any] = []
    provider = ScriptedProvider([shell_turn("ls /tmp"), "found two files"])
    outcome = runner(lambda task: seen.append(task) or FakeResult(stdout="a\nb")).run("go", provider)

    assert outcome.ok is True
    assert outcome.iterations == 2
    assert seen[0].kind == "shell"
    assert seen[0].payload == {"command": "ls /tmp"}
    assert outcome.tool_results[0]["content"] == "a\nb"


def test_the_transcript_has_the_expected_shape() -> None:
    provider = ScriptedProvider([shell_turn(), "done"])
    outcome = runner().run("go", provider)
    roles = [m["role"] for m in outcome.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assert outcome.messages[0]["content"] == DEFAULT_SYSTEM_PROMPT
    assert json.loads(outcome.messages[2]["tool_calls"][0]["function"]["arguments"]) == {"command": "ls"}


def test_a_custom_system_prompt_is_used() -> None:
    outcome = runner().run("hi", ScriptedProvider(["done"]), system_prompt="be terse")
    assert outcome.messages[0]["content"] == "be terse"


def test_an_empty_prompt_is_rejected() -> None:
    with pytest.raises(LlmTaskError, match="non-empty 'prompt'"):
        runner().run("   ", ScriptedProvider(["x"]))


def test_max_iterations_stops_a_looping_model() -> None:
    provider = ScriptedProvider([shell_turn(call_id=f"c{i}") for i in range(10)])
    outcome = runner().run("go", provider, max_iterations=3)
    assert outcome.stop_reason == "max_iterations"
    assert outcome.ok is False
    assert outcome.iterations == 3


def test_the_iteration_limit_is_clamped() -> None:
    provider = ScriptedProvider([shell_turn(call_id=f"c{i}") for i in range(200)])
    outcome = runner().run("go", provider, max_iterations=10_000)
    assert outcome.iterations <= MAX_ALLOWED_ITERATIONS


def test_a_zero_iteration_limit_still_runs_once() -> None:
    assert runner().run("hi", ScriptedProvider(["done"]), max_iterations=0).iterations == 1


def test_confirm_required_halts_the_loop() -> None:
    """A model must not be able to walk through a confirmation gate."""
    provider = ScriptedProvider([shell_turn("rm -rf /"), "carrying on regardless"])
    execute = lambda task: FakeResult(ok=False, decision="confirm_required", error="needs approval")
    outcome = runner(execute).run("go", provider)

    assert outcome.stop_reason == "confirm_required"
    assert outcome.ok is False
    assert outcome.pending_confirmation["tool"] == "shell"
    assert outcome.pending_confirmation["reason"] == "needs approval"
    assert outcome.iterations == 1  # it never got a second turn


def test_a_blocked_tool_halts_the_loop() -> None:
    execute = lambda task: FakeResult(ok=False, decision="block", error="policy says no")
    outcome = runner(execute).run("go", ScriptedProvider([shell_turn(), "ignored"]))
    assert outcome.stop_reason == "blocked"
    assert outcome.pending_confirmation["reason"] == "policy says no"


def test_a_provider_error_is_reported_not_raised() -> None:
    class BrokenProvider(Provider):
        name = "broken"

        def complete(self, messages: Sequence[dict], tools: Sequence[str]) -> ProviderResponse:
            raise RuntimeError("backend down")

    outcome = runner().run("go", BrokenProvider())
    assert outcome.stop_reason == "provider_error"
    assert outcome.final_message == "RuntimeError: backend down"


def test_a_raising_tool_becomes_a_tool_result() -> None:
    def boom(task: Any) -> Any:
        raise RuntimeError("kaboom")

    outcome = runner(boom).run("go", ScriptedProvider([shell_turn(), "done"]))
    assert outcome.tool_results[0]["decision"] == "error"
    assert "kaboom" in outcome.tool_results[0]["content"]
    assert outcome.stop_reason == "final"  # an error is observable, not fatal


def test_an_unavailable_tool_is_refused_without_executing() -> None:
    called: list[Any] = []
    provider = ScriptedProvider([{"tool_calls": [{"id": "c", "name": "memory_set"}]}, "done"])
    outcome = runner(lambda t: called.append(t)).run("go", provider)
    assert called == []
    assert outcome.tool_results[0]["decision"] == "unavailable_tool"


def test_the_tool_list_can_be_narrowed_per_run() -> None:
    called: list[Any] = []
    outcome = runner(lambda t: called.append(t)).run(
        "go", ScriptedProvider([shell_turn(), "done"]), tools=["read_file"]
    )
    assert called == []
    assert outcome.tool_results[0]["decision"] == "unavailable_tool"


def test_a_tool_outside_allowed_kinds_cannot_be_re_enabled() -> None:
    """`tools` narrows the allow-list; it never widens it."""
    called: list[Any] = []
    r = LlmTaskRunner(lambda t: called.append(t), allowed_kinds=["read_file"])
    outcome = r.run("go", ScriptedProvider([shell_turn(), "done"]), tools=["shell", "read_file"])
    assert called == []
    assert outcome.tool_results[0]["decision"] == "unavailable_tool"


def test_several_tool_calls_in_one_turn_all_run() -> None:
    seen: list[Any] = []
    provider = ScriptedProvider(
        [
            {"tool_calls": [
                {"id": "a", "name": "shell", "arguments": {"command": "one"}},
                {"id": "b", "name": "shell", "arguments": {"command": "two"}},
            ]},
            "done",
        ]
    )
    outcome = runner(lambda t: seen.append(t) or FakeResult()).run("go", provider)
    assert [t.payload["command"] for t in seen] == ["one", "two"]
    assert len(outcome.tool_results) == 2


def test_child_task_ids_are_derived_from_the_parent() -> None:
    seen: list[Any] = []
    parent = type("P", (), {"id": "llm-7", "envelope": None})()
    r = LlmTaskRunner(lambda t: seen.append(t) or FakeResult(), parent=parent)
    r.run("go", ScriptedProvider([shell_turn(call_id="c9"), "done"]))
    assert seen[0].id == "llm-7-tool-1-c9"


def test_stderr_is_used_when_there_is_no_stdout() -> None:
    execute = lambda t: FakeResult(stdout="", stderr="warning")
    outcome = runner(execute).run("go", ScriptedProvider([shell_turn(), "done"]))
    assert outcome.tool_results[0]["content"] == "warning"


def test_silent_tool_output_is_labelled() -> None:
    execute = lambda t: FakeResult(stdout="", stderr="", error="")
    outcome = runner(execute).run("go", ScriptedProvider([shell_turn(), "done"]))
    assert outcome.tool_results[0]["content"] == "(no output)"


def test_long_tool_output_is_truncated() -> None:
    execute = lambda t: FakeResult(stdout="x" * 9000)
    outcome = runner(execute).run("go", ScriptedProvider([shell_turn(), "done"]))
    assert len(outcome.tool_results[0]["content"]) == 4000


# ---------------------------------------------------------------------------
# Result rendering
# ---------------------------------------------------------------------------
def test_result_dict_counts_tool_calls() -> None:
    body = runner().run("go", ScriptedProvider([shell_turn(), "done"])).to_dict()
    assert body["tool_calls"] == 1
    assert body["stop_reason"] == "final"
    assert "pending_confirmation" not in body


def test_result_dict_includes_a_pending_confirmation() -> None:
    execute = lambda t: FakeResult(ok=False, decision="confirm_required", error="approve?")
    body = runner(execute).run("go", ScriptedProvider([shell_turn()])).to_dict()
    assert body["pending_confirmation"]["tool"] == "shell"


def test_every_stop_reason_the_loop_produces_is_declared() -> None:
    assert {"final", "max_iterations", "confirm_required", "blocked", "provider_error"} <= STOP_REASONS


def test_defaults_are_sane() -> None:
    assert 1 <= DEFAULT_MAX_ITERATIONS <= MAX_ALLOWED_ITERATIONS
    assert LlmTaskResult(ok=True, stop_reason="final", iterations=1, final_message="x").to_dict()["ok"]
