from __future__ import annotations

from typing import Any, Awaitable, Callable

from agent_framework import AgentResponse, AgentSession, Message
from agent_framework.orchestrations import GroupChatBuilder, GroupChatState


TurnCall = Callable[[str, int, list[Message], AgentSession | None], Awaitable[dict[str, Any]]]


class RoleParticipant:
    """Adapt one application role to Agent Framework's participant protocol."""

    def __init__(self, name: str, run_id: str, turn_call: TurnCall):
        self.id = self.name = name
        self.description = f"群聊角色 {name}"
        self._run_id = run_id
        self._turn_call = turn_call
        self._turn_index: Callable[[], int] | None = None

    def bind_turn_index(self, turn_index: Callable[[], int]) -> None:
        self._turn_index = turn_index

    def create_session(self, *, session_id: str | None = None) -> AgentSession:
        return AgentSession(session_id=session_id or f"{self._run_id}:{self.name}")

    def get_session(self, service_session_id: str, *, session_id: str | None = None) -> AgentSession:
        return AgentSession(service_session_id=service_session_id,
                            session_id=session_id or f"{self._run_id}:{self.name}")

    async def run(self, messages=None, *, stream: bool = False, session: AgentSession | None = None,
                  **_kwargs) -> AgentResponse:
        if stream:
            raise ValueError("群聊角色只使用完整消息模式")
        if self._turn_index is None:
            raise RuntimeError("群聊角色尚未绑定轮次")
        index = self._turn_index()
        result = await self._turn_call(self.name, index, list(messages or []), session)
        if session is not None:
            session.state["private_memory"] = result.get("_private_memory", "")
            session.state["last_seq"] = result["seq"]
        return AgentResponse(messages=Message(role="assistant", contents=[result["content"]],
                                              author_name=self.name,
                                              additional_properties={"seq": result["seq"]}))


async def run_group_chat(run: dict, planned: list[dict], turn_call: TurnCall) -> list[dict]:
    """Run a short planned segment through GroupChatBuilder, preserving application turn data."""
    output: list[dict] = []

    async def collect_turn(name: str, index: int, messages: list[Message], session: AgentSession | None) -> dict:
        if index != len(output) or planned[index]["speaker"] != name:
            raise ValueError("群聊编排顺序与角色执行顺序不一致")
        result = await turn_call(name, index, messages, session)
        output.append(result)
        return result

    names = list(run["agent_sessions"])
    participants = [RoleParticipant(name, run.get("id", "unsaved-run"), collect_turn) for name in names]
    for participant in participants:
        participant.bind_turn_index(lambda: len(output))

    def select(state: GroupChatState) -> str:
        if state.current_round != len(output):
            raise ValueError("群聊工作流轮次与已生成消息不一致")
        return planned[state.current_round]["speaker"]

    def finished(_conversation: list[Message]) -> bool:
        return len(output) >= len(planned) or bool(output and output[-1].get("reaction") in {"refuse", "leave"})

    workflow = GroupChatBuilder(participants=participants, selection_func=select,
                                termination_condition=finished, max_rounds=len(planned)).build()
    history = [Message(role="assistant", contents=[item["content"]], author_name=item["sender"],
                       additional_properties={"seq": item["seq"]}) for item in run["messages"][-18:]]
    if not history:
        history = [Message(role="user", contents=["开始群聊"])]
    await workflow.run(history)
    if not output or (len(output) != len(planned) and output[-1].get("reaction") not in {"refuse", "leave"}):
        raise ValueError("群聊工作流未完成计划轮次")
    return output
