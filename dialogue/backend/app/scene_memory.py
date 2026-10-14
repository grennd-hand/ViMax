"""Public, evidence-backed scene progress rebuilt from committed dialogue."""

from __future__ import annotations


def scene_ledger(run: dict) -> list[dict]:
    arc = run.get("story_arc") or []
    public = {item["seq"]: item for item in run.get("messages", [])}
    ledger: dict[int, dict] = {}
    for message in run.get("messages", []):
        events = message.get("story_events", [])
        if not isinstance(events, list):
            events = []
        if isinstance(message.get("story_event"), dict):
            events = events + [message["story_event"]]
        for event in events:
            if not isinstance(event, dict):
                continue
            event_id, state = event.get("id"), event.get("state")
            if type(event_id) is not int or not 0 <= event_id < len(arc) or state not in {"started", "resolved"}:
                continue
            previous = ledger.get(event_id)
            if previous and previous["state"] == "resolved":
                continue
            evidence_seq = event.get("evidence_seq", message["seq"])
            evidence = public.get(evidence_seq)
            if evidence is None or evidence_seq > message["seq"]:
                continue
            ledger[event_id] = {"id": event_id, "event": arc[event_id]["event"], "state": state,
                                "evidence": {"seq": evidence["seq"], "speaker": evidence["sender"],
                                             "content": evidence["content"]}}
            if event.get("outcome") in {"happened", "declined", "superseded"}:
                ledger[event_id]["outcome"] = event["outcome"]
    return list(ledger.values())


def apply_review_progress(run: dict, candidate: list[dict], updates: object) -> None:
    """Attach reviewer-checked progress only to public evidence in this accepted batch."""
    if not candidate or not isinstance(updates, list):
        return
    allowed_ids = {item["id"] for item in scene_candidates(run)}
    public = {item["seq"] for item in run.get("messages", [])}
    public.update(range(run["cursor"] + 1, run["cursor"] + len(candidate) + 1))
    for update in updates:
        if not isinstance(update, dict):
            continue
        event_id, state, seq = update.get("id"), update.get("state"), update.get("evidence_seq")
        if isinstance(event_id, str) and event_id.isdigit():
            event_id = int(event_id)
        evidence = seq if isinstance(seq, list) else [seq]
        evidence = [item for item in evidence if type(item) is int and item in public]
        if (type(event_id) is not int or event_id not in allowed_ids or state not in {"started", "resolved"}
                or not evidence):
            continue
        seq = max(evidence)
        message = candidate[seq - run["cursor"] - 1] if seq > run["cursor"] else candidate[-1]
        events = message.setdefault("story_events", [])
        if not any(item.get("id") == event_id for item in events):
            event = {"id": event_id, "state": state}
            if state == "resolved" and update.get("outcome") in {"happened", "declined", "superseded"}:
                event["outcome"] = update["outcome"]
            if seq <= run["cursor"]:
                event["evidence_seq"] = seq
            events.append(event)


def scene_candidates(run: dict) -> list[dict]:
    """Expose only unfinished events; closure is unavailable until the last three turns."""
    if run.get("story_arc_version", 0) < 3:
        return []
    resolved = {item["id"] for item in scene_ledger(run) if item["state"] == "resolved"}
    closing = run.get("cursor", 0) >= len(run.get("schedule", [])) - 3
    candidates = [{"id": index, **beat} for index, beat in enumerate(run.get("story_arc") or [])
                  if index not in resolved and (closing or beat.get("kind") != "closure")]
    if closing:
        endings = [item for item in candidates if item.get("kind") == "closure"]
        return endings or candidates[-1:]
    return candidates[:3]


def decision_ledger(run: dict) -> list[dict]:
    """Rebuild current public choices; revised or missing quotes invalidate updates."""
    public = {item["seq"]: item for item in run.get("messages", []) if "seq" in item}
    choices: dict[tuple[str, str], dict] = {}
    for message in run.get("messages", []):
        for update in message.get("decision_updates", []):
            if not isinstance(update, dict):
                continue
            role, subject = update.get("role"), update.get("subject")
            seq = update.get("evidence_seq")
            evidence = public.get(seq) if type(seq) is int else None
            if (not isinstance(role, str) or not isinstance(subject, str) or not subject
                    or evidence is None or evidence.get("sender") != role
                    or seq > message.get("seq", 0) or update.get("quote") != evidence.get("content")):
                continue
            choices[role, subject] = {"role": role, "subject": subject, "summary": update.get("summary", ""),
                                      "evidence": {"seq": seq, "content": evidence["content"]}}
    return sorted(choices.values(), key=lambda item: item["evidence"]["seq"])


def apply_review_decisions(run: dict, candidate: list[dict], updates: object) -> None:
    """Accept decisions only from the speaker's actual lines in this accepted batch."""
    if not candidate or not isinstance(updates, list):
        return
    for update in updates[:16]:
        if not isinstance(update, dict):
            continue
        seq = update.get("evidence_seq")
        if type(seq) is not int or not run["cursor"] < seq <= run["cursor"] + len(candidate):
            continue
        message = candidate[seq - run["cursor"] - 1]
        role = update.get("role")
        subject = " ".join(str(update.get("subject") or "").split())
        summary = " ".join(str(update.get("summary") or "").split())
        if role != message.get("sender") or not 1 <= len(subject) <= 80 or not 1 <= len(summary) <= 220:
            continue
        record = {"role": role, "subject": subject, "summary": summary,
                  "evidence_seq": seq, "quote": message["content"]}
        attached = message.setdefault("decision_updates", [])
        if not any(item.get("subject") == subject for item in attached):
            attached.append(record)


def apply_review_actions(run: dict, candidate: list[dict], updates: object, workflow_count: int) -> None:
    """Use the critic's exact quote to record actions without rewriting dialogue."""
    if not isinstance(updates, list):
        return
    for update in updates[:32]:
        if not isinstance(update, dict) or type(update.get("seq")) is not int:
            continue
        seq = update["seq"]
        if not run["cursor"] < seq <= run["cursor"] + len(candidate):
            continue
        message = candidate[seq - run["cursor"] - 1]
        steps = update.get("steps")
        if (message.get("sender") == "坐席" or update.get("quote") != message.get("content")
                or not isinstance(steps, list)
                or any(type(step) is not int or not 0 <= step < workflow_count for step in steps)):
            continue
        message["performed_steps"] = sorted(set(steps))
        message["action_evidence"] = {"quote": message["content"], "steps": message["performed_steps"]}
        message["_private_memory"] = f"本人公开原话：{message['content']}；本句完成步骤：{message['performed_steps']}"[:500]
