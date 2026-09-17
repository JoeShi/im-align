"""User Simulator core for the Backend Smoke layer (docs/e2e-harness-design.md).

A langgraph graph polling the Alignment Thread over Feishu OpenAPI (no long
connection, so it never contends with the Bridge for the single-session lock):

    ingest(thread message)
      -> classify(question | progress | complete_card | error)
      |- question      -> stage; after QUESTION_QUIET_ROUNDS quiet polls
      |                 (no new messages) -> compose_answer(temperature 0) -> post reply
      |- progress      -> record
      |- complete_card -> record -> end
      |- error         -> record -> tolerate up to N, then escalate

A question is staged instead of answered immediately because the Bridge posts
one Turn's output as several sequential card messages; answering on the first
question card would interleave the reply with chunks still being posted. The
quiet-period gate also absorbs leftover announcements.

The Bridge delivers every Turn output as a card, so question detection cannot
rely on msg_type alone: a card body containing a question mark (?, ？, or the
grilling skill's ❓) classifies as a question.

There is deliberately no approval_card branch: per ADR-0005 the happy path
must produce zero Approval cards, so an approval card is itself a
deterministic failure and terminates the run.

LLM calls and Feishu OpenAPI calls go through transports.ThreadTransport and
transports.SimulatorLLMTransport so tests inject fakes; nothing here needs
real credentials.
"""

import json
import logging
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .transports import SimulatorLLMTransport, ThreadMessage, ThreadTransport

log = logging.getLogger("im_align.e2e.user_simulator")

MAX_TOLERATED_ERRORS = 3

# A staged question is answered only after this many consecutive polls with no
# new Thread messages. The runner sleeps one poll interval between invokes, so
# 2 means "the Thread was silent for ~2 poll intervals after the last chunk".
QUESTION_QUIET_ROUNDS = 2

# These are structural Bridge titles, preserved separately from Agent prose
# when the API response is flattened for the simulator LLM.
_CONTROL_TITLES = {
    "🔐 Approval Request": "approval_card",
    "✅ Alignment Complete": "complete_card",
    "❌ Error": "error",
}

# Question marks accepted in both half- and full-width forms plus the grilling
# skill's ❓ question emoji; the Bridge's thinking/progress cards contain none.
_QUESTION_MARKS = ("?", "？", "❓")


class SimulatorState(TypedDict, total=False):
    message: ThreadMessage | None  # polled message; None means the Thread is idle
    message_type: str  # question | progress | complete_card | error | approval_card | idle
    user_brief: str
    answer: str
    replies: list
    reply_receipts: list
    events: list
    error_count: int
    pending_question: str  # staged question awaiting the quiet period
    quiet_rounds: int  # consecutive idle polls since the last staged question
    terminal: str  # "" | "complete" | "escalated" | "approval_card_detected" | "idle"


def classify_message(message: ThreadMessage) -> str:
    """Deterministic message classification; pure function, unit-tested."""
    # Agent prose can quote approval/completion language. Only the structural
    # Bridge card title carries lifecycle meaning; never scan the body for it.
    if message.msg_type in ("interactive", "card") and message.card_title in _CONTROL_TITLES:
        return _CONTROL_TITLES[message.card_title]
    text = message.text or ""
    if any(mark in text for mark in _QUESTION_MARKS):
        return "question"
    return "question" if message.msg_type == "text" else "progress"


def ingest_node(state: SimulatorState, thread: ThreadTransport) -> dict:
    message = thread.poll()
    if message is None:
        return {"message": None, "message_type": "idle"}
    message_type = classify_message(message)
    log.info(
        "simulator ingest: msg=%s type=%s text=%r",
        message.message_id,
        message_type,
        (message.text or "")[:80],
    )
    return {"message": message, "message_type": message_type}


def stage_question_node(state: SimulatorState) -> dict:
    # Hold the question until the Thread goes quiet; the newest staged
    # question wins, matching how the Bridge chunks one Turn's output.
    log.info("simulator staged question, waiting for quiet period")
    out = record_node(state)
    out["pending_question"] = state["message"].text
    out["quiet_rounds"] = 0
    return out


def count_quiet_node(state: SimulatorState) -> dict:
    return {"quiet_rounds": int(state.get("quiet_rounds") or 0) + 1}


def compose_answer_node(state: SimulatorState, llm: SimulatorLLMTransport) -> dict:
    question = state.get("pending_question") or ""
    if not question and state.get("message") is not None:
        question = state["message"].text
    log.info("simulator quiet period elapsed; composing answer")
    answer = llm.compose_answer(question, state["user_brief"])
    log.info("simulator answer composed (%d chars)", len(answer))
    return {"answer": answer}


def post_reply_node(state: SimulatorState, thread: ThreadTransport) -> dict:
    receipt = thread.post_reply(state["answer"])
    log.info("simulator posted reply msg=%s", receipt.message_id)
    replies = list(state.get("replies") or [])
    replies.append(state["answer"])
    reply_receipts = list(state.get("reply_receipts") or [])
    reply_receipts.append(
        {
            "message_id": receipt.message_id,
            "create_time": receipt.create_time,
        }
    )
    return {
        "replies": replies,
        "reply_receipts": reply_receipts,
        "answer": "",
        "pending_question": "",
        "quiet_rounds": 0,
    }


def record_node(state: SimulatorState) -> dict:
    events = list(state.get("events") or [])
    events.append(
        {
            "type": state["message_type"],
            "message_id": state["message"].message_id,
            "text": state["message"].text,
        }
    )
    return {"events": events}


def record_error_node(state: SimulatorState) -> dict:
    error_count = int(state.get("error_count") or 0) + 1
    out = record_node(state)
    out["error_count"] = error_count
    return out


def escalate_node(state: SimulatorState) -> dict:
    return {"terminal": "escalated"}


def terminal_node(state: SimulatorState) -> dict:
    return {"terminal": "complete"}


def approval_detected_node(state: SimulatorState) -> dict:
    # ADR-0005 invariant: an Approval card on the happy path is a product bug.
    return {"terminal": "approval_card_detected"}


def route_message_type(state: SimulatorState) -> str:
    return state["message_type"]


def route_after_error(state: SimulatorState) -> str:
    if int(state.get("error_count") or 0) >= MAX_TOLERATED_ERRORS:
        return "escalate"
    return "continue"


def route_after_record(state: SimulatorState) -> str:
    # progress records and keeps polling; only the completion card ends the run.
    if state["message_type"] == "complete_card":
        return "terminal"
    return "continue"


def route_after_quiet(state: SimulatorState) -> str:
    # Answer only once the Thread has been silent for the quiet period; any
    # new message (including a completion card) preempts the staged question.
    if state.get("pending_question") and (
        int(state.get("quiet_rounds") or 0) >= QUESTION_QUIET_ROUNDS
    ):
        return "answer"
    return "wait"


def build_graph(thread: ThreadTransport, llm: SimulatorLLMTransport):
    """Compile the User Simulator graph against injected transports."""
    builder = StateGraph(SimulatorState)
    builder.add_node("ingest", lambda s: ingest_node(s, thread))
    builder.add_node("stage_question", stage_question_node)
    builder.add_node("count_quiet", count_quiet_node)
    builder.add_node("compose_answer", lambda s: compose_answer_node(s, llm))
    builder.add_node("post_reply", lambda s: post_reply_node(s, thread))
    builder.add_node("record", record_node)
    builder.add_node("record_error", record_error_node)
    builder.add_node("escalate", escalate_node)
    builder.add_node("terminal", terminal_node)
    builder.add_node("approval_detected", approval_detected_node)

    builder.add_edge(START, "ingest")
    builder.add_conditional_edges(
        "ingest",
        route_message_type,
        {
            "idle": "count_quiet",
            "question": "stage_question",
            "progress": "record",
            "complete_card": "record",
            "error": "record_error",
            "approval_card": "approval_detected",
        },
    )
    builder.add_edge("stage_question", END)
    builder.add_conditional_edges(
        "count_quiet",
        route_after_quiet,
        {"answer": "compose_answer", "wait": END},
    )
    builder.add_edge("compose_answer", "post_reply")
    builder.add_edge("post_reply", END)
    builder.add_conditional_edges(
        "record_error",
        route_after_error,
        {"escalate": "escalate", "continue": END},
    )
    builder.add_conditional_edges(
        "record",
        route_after_record,
        {"terminal": "terminal", "continue": END},
    )
    builder.add_edge("terminal", END)
    builder.add_edge("escalate", END)
    builder.add_edge("approval_detected", END)
    return builder.compile()


def run_simulator_turn(graph, state: SimulatorState) -> SimulatorState:
    """Execute one polling iteration; the outer process loop calls this."""
    final = graph.invoke(state)
    return final


def simulator_loop(graph, user_brief: str, max_turns: int = 200) -> SimulatorState:
    """Poll until the Thread goes terminal; guard bound keeps CI from hanging.

    Exhausting max_turns without a terminal state returns the state as-is; a
    silent Thread is the outer process's wall-clock timeout to detect, while
    only repeated error cards escalate through the graph itself.
    """
    state: SimulatorState = {
        "message": None,
        "message_type": "",
        "user_brief": user_brief,
        "answer": "",
        "replies": [],
        "reply_receipts": [],
        "events": [],
        "error_count": 0,
        "pending_question": "",
        "quiet_rounds": 0,
        "terminal": "",
    }
    for _ in range(max_turns):
        state = run_simulator_turn(graph, state)
        if state.get("terminal"):
            return state
    return state


def transcript_export(state: SimulatorState) -> str:
    """Serialize observed events for the Run Evaluator evidence bundle."""
    return json.dumps(
        {
            "events": state.get("events") or [],
            "replies": state.get("replies") or [],
            "reply_receipts": state.get("reply_receipts") or [],
            "error_count": state.get("error_count") or 0,
            "terminal": state.get("terminal") or "",
        },
        ensure_ascii=False,
        indent=2,
    )
