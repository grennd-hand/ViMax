"""Small, evidence-linked memories for each simulated person.

Stable personality is stored in the persona card. These events are only what
the person publicly did, decided, or heard in this particular run.
"""

from __future__ import annotations

import re


def _append(session: dict, event: dict) -> None:
    events = session.setdefault("memory_events", [])
    if not any(item["seq"] == event["seq"] and item["kind"] == event["kind"] for item in events):
        events.append(event)


def record_public_memory(sessions: dict[str, dict], message: dict, history: list[dict]) -> None:
    """Record accepted public events, never private guesses or rejected drafts."""
    sender = message["sender"]
    if sender not in sessions:
        return
    seq = message["seq"]
    content = str(message.get("content") or "")[:160]
    own = sessions[sender]
    steps = message.get("performed_steps") or []
    if steps:
        _append(own, {"seq": seq, "kind": "own_action", "step_ids": steps, "text": content})
    else:
        _append(own, {"seq": seq, "kind": "own_statement", "text": content})
    state = message.get("task_state")
    if state in {"active", "paused", "declined", "submitted"} and state != own.get("last_task_state"):
        _append(own, {"seq": seq, "kind": "own_decision", "state": state, "text": content})
        own["last_task_state"] = state

    peer = None
    reply_to = message.get("reply_to")
    if isinstance(reply_to, int):
        original = next((item for item in reversed(history) if item.get("seq") == reply_to), None)
        if original and original.get("sender") != sender:
            peer = original.get("sender")
    if peer is None:
        peer = next((name for name in sessions if name != sender and
                     any(content.startswith(name + mark) for mark in ("，", ",", "：", ":"))), None)
    if peer in sessions:
        _append(own, {"seq": seq, "kind": "said_to", "peer": peer, "text": content})
        _append(sessions[peer], {"seq": seq, "kind": "heard_from", "peer": sender, "text": content})


def _terms(text: str) -> set[str]:
    # Local lexical retrieval; semantic cross-language recall can be supplied by
    # an embedding adapter later. Neither path needs to rewrite source quotes.
    words = re.findall(r"[^\W_]+", text.casefold())
    return {word for word in words} | {word[index:index + 2] for word in words
                                      if re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", word)
                                      for index in range(len(word) - 1)}


def select_memory(events: list[dict], recent_peers: set[str], limit: int = 8, query: str = "") -> list[dict]:
    """Select by current relevance, importance and recency; keep the complete archive."""
    if not events or limit <= 0:
        return []
    query_terms = _terms(query)
    latest = max(item["seq"] for item in events)

    def score(item: dict) -> float:
        terms = _terms(str(item.get("text") or ""))
        relevance = len(query_terms & terms) / max(1, len(terms)) if query_terms else 0
        importance = {"own_action": .8, "own_decision": 1., "heard_from": .6, "said_to": .5}.get(item["kind"], .3)
        return 3 * relevance + importance + (1 if item.get("peer") in recent_peers else 0) + .99 ** (latest - item["seq"])

    anchors = ([item for item in events if item["kind"] == "own_action"][-2:] +
               [item for item in events if item["kind"] == "own_decision"][-1:])
    selected: dict[int, dict] = {}
    for item in anchors + sorted(events, key=score, reverse=True):
        if len(selected) >= limit:
            break
        selected.setdefault(item["seq"], item)
    return sorted(selected.values(), key=lambda item: item["seq"])
