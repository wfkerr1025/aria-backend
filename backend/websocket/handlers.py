# backend/websocket/handlers.py

from __future__ import annotations
import json
import re
import asyncio
import functools
import time

from backend.core.provider_router import ProviderRouter
from backend.core.streaming_engine import StreamingEngine
from backend.core.local_inference_engine import InferenceRequest, InferenceMessage

from backend.core.answer_stream import AnswerStream
from backend.core.action_stream_filter import ActionBlockFilter
from backend.core.action_plan import requests_live_execution
from backend.core import turn_status
from backend.chat import supervisor_layer
from backend.core import action_render

# Hold every token until the whole reply exists, then send it as one
# piece. OFF.
#
# It was on, and on a 12B it read as a freeze: 55 seconds of blank screen
# for one file, most of that a cold model load with nothing to show. A
# supervision pass is not worth a minute of silence.
#
# Streaming is back, and the cleanup did not have to be given up for it.
# The provider's stream_end is held, the actions run, and a
# message_revised packet replaces the bubble with the checked text before
# the stream is closed. The user reads tokens as they arrive and still
# ends the turn with the tidied answer.
#
# Set True to go back to buffering, where nothing is shown until the
# whole reply has been supervised.
BUFFER_FOR_SUPERVISION = False


from backend.core.turn_orchestrator import orchestrate_turn
from backend.core.tool_orchestrator import run_answer_actions
from backend.core.turn_types import (
    KIND_CLARIFY,
    KIND_ERROR,
    KIND_MODEL_SWITCH,
    KIND_SAFETY_WARNING,
    KIND_TEXT,
    SessionState,
    TurnRequest,
)
from backend.core.lighter_model_engine import LighterModelEngine
from backend.core import model_manager
from backend.core import key_manager
from backend.core import warning_manager
from backend.core import auto_balancer
from backend.core import routing_guard
from backend.core import tool_router
from backend.core import connection_state
from backend.core import backend_watchdog
from backend.core.runtime_health_monitor import RuntimeHealthMonitor

from backend.ipc_router import dispatch as ipc_dispatch, HANDLED_TYPES as IPC_HANDLED_TYPES
from backend import ipc_errors
from backend import ipc_schema as schema

from backend.core.conversation_manager import (
    log_context_reset,
    log_history_policy,
    log_intent_detected,
    log_response_optimized,
    log_self_query_resolved,
    log_system_prompt_applied,
    log_tool_routing_decision,
    new_conversation_id,
    optimize_response,
)

from backend.logger import log as unified_log

from logger import get_logger

logger = get_logger(__name__)


_WRITING_PATH = re.compile(r'"path"\s*:\s*"([^"]{1,200})"')


def _path_being_written(raw_so_far: str):
    """The file named in the action block the model is part-way through.

    Read off the raw text as it arrives, so the progress line can say
    "writing player_inventory.cs" instead of "writing the file" as soon
    as the model has got that far -- which is usually within a token or
    two of opening the block, and long before the contents are done.
    """
    match = _WRITING_PATH.search(str(raw_so_far or ""))
    return match.group(1) if match else None


def _failed_paths(report) -> dict:
    """Paths whose action did not run, with the reason it did not.

    Read off the RESULTS, not the proposal. Rendering the proposal alone
    reported a refused staging as "Staged: - write ...", which is ARIA
    claiming work it had not done.
    """
    if not report:
        return {}

    by_step = {a.get("step_id"): a for a in report.get("actions") or []}
    failed = {}
    for result in report.get("results") or []:
        if result.get("status") in ("ok", "skipped"):
            continue
        action = by_step.get(result.get("step_id")) or {}
        path = str((action.get("args") or {}).get("path") or "")
        if path:
            failed[path] = result.get("error") or "it did not run"
    return failed


def _expects_an_action(user_text: str) -> bool:
    """Whether the user asked for file work on this turn."""
    try:
        from backend.chat.model_router import TURN_TOOLS, classify_turn

        class _Request:
            latest_user_text = user_text
            messages = ()

        return classify_turn(_Request()) == TURN_TOOLS
    except Exception:  # pragma: no cover - a note is not worth a turn
        logger.exception("could not classify the turn for the missing-action note")
        return False



class WebSocketHandler:
    """
    Handles WebSocket messages from the frontend.
    Converts incoming packets → InferenceRequest
    Routes inference through ProviderRouter
    Streams tokens via StreamingEngine
    """

    def __init__(self, websocket):
        self.websocket = websocket
        self.router = ProviderRouter()
        self.streamer = StreamingEngine()
        self.suggester = LighterModelEngine()

        # Every connection starts as its own conversation; a context_reset
        # packet (or the client sending a fresh conversationId) rotates it.
        self.conversation_id = new_conversation_id()

        # This turn's user message, kept for the action pass after
        # streaming. Empty rather than absent so a stream that never went
        # through _dispatch reads as "no consent given", which is the
        # safe answer and the one a dry run needs.
        self._turn_user_text = ""
        self._turn_expects_action = False

        # Set by _handle_model_override ("Proceed Anyway") to the exact
        # model_id the user just accepted the warning for. Consumed
        # (one-shot) by the very next chat_request for that same
        # model_id, so it doesn't immediately re-trigger the same
        # safety_warning it was meant to bypass. Not a role change and
        # not session-wide like skipSafetyCheck — a different model_id,
        # or the same one a second time, goes through evaluate_safety
        # normally again.
        self._override_model_id = None

        # Warning system (backend.core.warning_manager) — per-connection
        # dedup state for the once-per-session "normal" warnings, plus
        # the idle/background health poller that makes critical
        # warnings (sustained CPU, RAM, thermal throttling) detectable
        # even between chat turns. Started in handle() (connection-
        # lifetime scope), stopped in its finally block.
        self._warning_session_state = warning_manager.new_session_state()
        self._health_monitor = RuntimeHealthMonitor()

        # Batch 3 — tool/weather truth alignment: set False the moment a
        # send over THIS connection actually fails (see _send()'s except
        # branch below) — the same real signal Batch 1's heartbeat
        # sender depends on to prove the connection is alive. Consulted
        # by _dispatch() before routing a tool_execute_request through
        # ipc_router/tool_router, so a tool never runs (or appears to
        # run) once this connection is known to be down.
        self._connection_healthy = True

        # Batch 3.6 — weather clarification/correction state machine.
        # _awaiting_weather_location: True right after ARIA asked "which
        # location's weather do you want?" — the VERY NEXT user message
        # is treated as a bare location answer (no "weather in X"
        # phrasing required), never re-run through detect_intent().
        # _last_turn_was_weather: True right after any weather answer
        # (or clarification prompt) — lets a correction phrase ("that is
        # incorrect, try again") re-ask for location instead of falling
        # through to ordinary LLM chat, which is what used to let a real
        # model hallucinate a "corrected" weather value from its own
        # training data instead of a real fusion-engine lookup. Reset to
        # False the moment the conversation moves on to anything else —
        # see _handle_chat_request().
        self._awaiting_weather_location = False
        self._last_turn_was_weather = False

        logger.info("WebSocketHandler initialized. conversation_id=%s", self.conversation_id)

    # -----------------------------------------------------
    # Main entry point for incoming messages
    # -----------------------------------------------------
    async def handle(self):
        logger.debug("Starting async message loop.")

        self._health_monitor.start()
        connection_state.connection_opened()
        backend_watchdog.watchdog.start()

        # Batch 4 — "On reconnect: restore routing state, emit fresh
        # truth packets". restore_state() with no arguments is safe and
        # idempotent to call on every connection open, not just after an
        # actual process restart: it re-validates whatever's currently
        # persisted (never trusts a possibly-stale in-memory copy) and
        # only writes anything if that state turns out to be genuinely
        # invalid — see its own docstring for why this can never clobber
        # a legitimately newer live state. mode_status_result and
        # diagnostics_backend_result are pushed unsolicited so a
        # (re)connecting client never has to ask first and never
        # renders on a stale local default while waiting for a reply.
        backend_watchdog.watchdog.restore_state()
        await self._send(ipc_dispatch({"type": schema.MODE_STATUS_REQUEST, "payload": {}}))
        await self._send(ipc_dispatch({"type": schema.DIAGNOSTICS_BACKEND_REQUEST, "payload": {}}))

        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            async for raw in self.websocket:
                logger.debug("Raw incoming packet: %s", raw)

                # Two separate try/excepts on purpose — JSON-parse failures
                # and dispatch failures are different problems with
                # different causes, and collapsing them into one generic
                # "Invalid packet" catch (as this used to do) mislabels
                # dispatch bugs as parse errors and leaks raw Python
                # exception text (e.g. "unhashable type: 'dict'") to the
                # client instead of a clear diagnostic. Both nets exist so
                # that no single malformed packet — whatever kind of
                # malformed — can ever kill this connection's message loop
                # or leave state that poisons the next packet.
                try:
                    packet = self._safe_json(raw)
                except Exception as e:
                    logger.exception("JSON parse error: %s", e)
                    unified_log("websocket", "ERROR", f"JSON parse error: {e}")
                    await self._send({
                        "type": "error",
                        "message": f"Invalid JSON: {str(e)}"
                    })
                    continue

                try:
                    logger.debug("Parsed packet: %s", packet)
                    await self._dispatch(packet)
                except Exception as e:
                    logger.exception("Dispatch error: %s", e)
                    unified_log("websocket", "ERROR", f"Dispatch error: {e}", {
                        "packet_repr": repr(packet)[:500],
                    })
                    await self._send({
                        "type": "error",
                        "message": f"Dispatch error: {str(e)}"
                    })
        finally:
            logger.debug("Exiting async message loop.")
            heartbeat_task.cancel()
            self._health_monitor.stop()
            connection_state.connection_closed()
            backend_watchdog.watchdog.mark_disconnected()

    # -----------------------------------------------------
    # Batch 1 stability fix — proactive backend heartbeat. Previously
    # HEARTBEAT/HEARTBEAT_ACK only existed as a client-initiated
    # keepalive (see backend/ipc_router.py's _handle_heartbeat()); the
    # frontend had no way to detect a backend that's silently hung
    # (process alive, socket still open, but stuck) short of that same
    # client sending a heartbeat and never getting an ack — nothing made
    # that automatic. This sends an unsolicited heartbeat every interval
    # for the connection's lifetime so webui/core/bridge.js can track
    # elapsed time since the last one and mark the backend "disconnected"
    # (blocking new model/mode/tool requests) purely from silence, without
    # depending on a TCP-level disconnect ever happening.
    # -----------------------------------------------------
    HEARTBEAT_INTERVAL_SECONDS = 10.0

    async def _heartbeat_loop(self):
        try:
            while True:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL_SECONDS)
                await self._send({"type": schema.HEARTBEAT, "ts": time.time()})
                if self._connection_healthy:
                    # Only record on an actually-successful send — _send()
                    # sets _connection_healthy False on failure, so a
                    # heartbeat that never really went out never counts as
                    # proof of life (see connection_state.py).
                    connection_state.record_heartbeat_sent()
                    backend_watchdog.watchdog.record_heartbeat()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Never let a heartbeat-send failure kill the connection —
            # the main message loop is the only thing allowed to end it.
            logger.debug(f"_heartbeat_loop() — send failed: {e}")

    # -----------------------------------------------------
    # Safe JSON loader
    # -----------------------------------------------------
    def _safe_json(self, raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")

        try:
            return json.loads(raw)
        except Exception:
            logger.exception("Malformed JSON: %s", raw)
            raise ValueError(f"Malformed JSON: {raw}")

    # -----------------------------------------------------
    # Dispatch packet types
    # -----------------------------------------------------
    async def _dispatch(self, packet: dict):
        # The dispatch key must always come from packet["type"] as a
        # plain string — never trust it to already be one. A client bug
        # like `bridge.send({type: "load_model_override", model_id})`
        # (an object passed where bridge.send(type, payload) expects a
        # string) produces a packet shaped {"type": {"type": "...", ...},
        # "payload": {}} — packet["type"] here is a dict, and dict is
        # unhashable, so `ptype in IPC_HANDLED_TYPES` (a frozenset) or a
        # dict-key lookup on it would raise TypeError. Validating first
        # turns that into one clear, logged, non-fatal error response
        # instead of letting an unhashable value reach a set/dict lookup.
        if not isinstance(packet, dict):
            logger.error("Malformed packet: expected a JSON object, got %s: %r", type(packet).__name__, packet)
            unified_log("websocket", "ERROR", "Malformed packet: not an object", {
                "received_type": type(packet).__name__,
            })
            await self._send({
                "type": "error",
                "message": f"Malformed packet: expected an object, got {type(packet).__name__}",
            })
            return

        ptype = packet.get("type")

        if not isinstance(ptype, str) or not ptype:
            logger.error("Malformed packet: 'type' must be a non-empty string, got %r. packet=%r", ptype, packet)
            unified_log("websocket", "ERROR", "Malformed packet: 'type' missing or not a string", {
                "type_value_repr": repr(ptype)[:200],
                "packet_keys": list(packet.keys()),
            })
            await self._send({
                "type": "error",
                "message": "Malformed packet: 'type' must be a non-empty string",
            })
            return

        logger.debug("Dispatching packet type: %s", ptype)
        unified_log("websocket", "INFO", f"Incoming packet: {ptype}", {"packet_type": ptype})

        # webui/core/bridge.js's send(type, payload) always wraps the
        # caller's fields as {"type": type, "payload": payload} on the
        # wire — every bridge.send() call in the frontend (chat_request,
        # switch_to_lighter_model, load_model_override, context_reset)
        # sends its actual fields (messages, model_id, conversationId,
        # skipSafetyCheck, ...) nested under "payload", never at the top
        # level. The handlers below (_handle_chat_request,
        # _handle_switch_to_lighter_model, _handle_model_override,
        # _handle_context_reset) have always read those fields straight
        # off the packet root — so every one of those requests from the
        # real frontend was silently seeing empty/missing fields (empty
        # `messages`, `None` `model_id`), which is what actually produced
        # `unknown_intent` self-queries and "missing model_id" errors on
        # switch/override, not a bug in intent detection or model
        # switching themselves. Only backend.ipc_router already unwrapped
        # "payload" itself (it owns the model_set_active_request /
        # models_list_request family), which is why that path always
        # worked. Flattening it once here, uniformly, fixes every
        # dispatch() caller at once and leaves ipc_dispatch()'s own
        # separate payload extraction below untouched (it reads the same
        # now-redundant but still-present "payload" key). Top-level
        # fields win on conflict, so flat packets — every packet this
        # file's own regression tests construct, and any raw non-Electron
        # client — pass through unchanged.
        if isinstance(packet.get("payload"), dict):
            packet = {**packet["payload"], **packet}

        if ptype == "shutdown":
            logger.info("Shutdown command received.")
            await self.websocket.close()
            loop = asyncio.get_event_loop()
            loop.stop()
            return

        if ptype == schema.HEARTBEAT:
            # A client-initiated heartbeat (as opposed to this
            # connection's own proactive send — see _heartbeat_loop()) is
            # equally real proof of life; falls through to the normal
            # IPC_HANDLED_TYPES branch below for the actual heartbeat_ack.
            backend_watchdog.watchdog.record_heartbeat()

        if ptype == "chat_request":
            logger.debug("chat_request received.")
            await self._handle_chat_request(packet)
            return

        if ptype == "load_model_override":
            logger.debug("load_model_override received.")
            await self._handle_model_override(packet)
            return

        if ptype == "switch_to_lighter_model":
            logger.debug("switch_to_lighter_model received.")
            await self._handle_switch_to_lighter_model(packet)
            return

        if ptype == "context_reset":
            logger.debug("context_reset received.")
            await self._handle_context_reset(packet)
            return

        # Batch 3 — "Tools must not run during backend disconnect": gated
        # here, before ipc_router/tool_registry ever runs the handler,
        # rather than inside ipc_router.dispatch() itself — dispatch() is
        # a shared, connection-agnostic function with no notion of which
        # of possibly several concurrent connections called it, while
        # this WebSocketHandler is the one thing that actually knows
        # THIS connection's health (see _connection_healthy).
        if ptype == schema.TOOL_EXECUTE_REQUEST and not self._connection_healthy:
            logger.warning("tool_execute_request rejected — connection unhealthy.")
            await self._send(ipc_errors.build_error(
                tool_router.BACKEND_DISCONNECTED,
                "Backend connection is down — tool requests are not accepted until it recovers.",
                schema.TOOL_EXECUTE_REQUEST,
            ))
            return

        if ptype in IPC_HANDLED_TYPES:
            logger.debug("Routing %s through ipc_router.", ptype)
            response = ipc_dispatch(packet)
            await self._send(response)
            return

        logger.warning("Unknown packet type: %s", ptype)
        await self._send({
            "type": "error",
            "message": f"Unknown packet type: {ptype}"
        })

    # -----------------------------------------------------
    # Handle chat_request from frontend
    # -----------------------------------------------------
    async def _handle_chat_request(self, packet: dict):
        """Run one turn through the shared orchestrator and apply the result.

        Every decision this used to make inline -- the weather continuation
        window, intent short-circuits, model precedence, absolute mode
        separation, the safety gate, history policy -- now lives in
        backend/core/turn_orchestrator.py, which REST calls too. What is
        left here is what only a transport can do: apply session state,
        emit telemetry, and put bytes on the socket.

        orchestrate_turn is synchronous and some of what it calls blocks (a
        weather lookup, a web search, retrieval), so it runs through
        run_in_executor. One wrapper at one call site, rather than an async
        colouring spreading through every branch of the sequence.
        """
        conversation_id = packet.get("conversationId") or self.conversation_id
        self.conversation_id = conversation_id

        # Kept for the action pass at the end of streaming. Consent to
        # apply an action is read from what the USER said, and by the
        # time the answer exists the packet is long out of scope.
        self._turn_user_text = self._latest_user_text(packet)

        # Whether this turn was routed as file work. Read here, from the
        # same classifier the router uses, so the delivery path can tell
        # "answered a question" from "was asked to act and did not".
        self._turn_expects_action = _expects_an_action(self._turn_user_text)

        request = TurnRequest(
            messages=packet.get("messages", []),
            latest_user_text=self._latest_user_text(packet),
            conversation_id=conversation_id,
            multi_turn=packet.get("multiTurn", True),
            requested_model_id=packet.get("modelId"),
            skip_safety_check=bool(packet.get("skipSafetyCheck", False)),
            allow_override=bool(packet.get("allowOverride", False)),
            max_tokens=packet.get("maxTokens", 2048),
            temperature=packet.get("temperature", 0.7),
            session=self._session_snapshot(),
        )

        # The whole orchestration phase reads as one state from out here:
        # it is a single synchronous call, and everything it does inside
        # -- retrieval, the plan, a web lookup, synthesis -- finishes
        # before it returns. See backend/core/turn_status.py for why the
        # finer states are defined but not sent.
        await self._emit_status(turn_status.THINKING)

        loop = asyncio.get_running_loop()

        def forward_status(value: str) -> None:
            # Called from the executor thread, so the send is handed back
            # to the loop the same way stream tokens are.
            asyncio.run_coroutine_threadsafe(self._emit_status(value), loop)

        result = await loop.run_in_executor(
            None,
            functools.partial(
                orchestrate_turn,
                request,
                mode_manager=self.router.mode_manager,
                suggester=self.suggester,
                on_status=forward_status,
            ),
        )

        self._apply_session_updates(result.session_updates)
        self._emit_telemetry(result.telemetry, conversation_id)

        # Warnings are emitted on every turn that resolved a model, not
        # only refusing ones -- unchanged from before the extraction.
        if result.model_cfg is not None and result.safety_decision is not None:
            await self._emit_warnings(result.model_cfg, result.safety_decision)

        # Notices accompany a turn rather than replacing it: today, the
        # chat capability gate saying it moved this turn onto a model that
        # can follow the protocol. Sent before the answer, so the reason
        # is on screen by the time the different model's text arrives --
        # otherwise the switch looks like ARIA ignoring the model the user
        # picked.
        for notice in result.notices:
            await self._send({"type": "warning_event", **notice})

        if result.kind == KIND_SAFETY_WARNING:
            await self._send(result.warning)
            await self._emit_status(turn_status.IDLE)
            return

        if result.kind == KIND_ERROR:
            await self._send(ipc_errors.build_error(
                tool_router.BACKEND_DISCONNECTED, result.text,
            ))
            await self._emit_status(turn_status.IDLE)
            return

        if result.kind == KIND_MODEL_SWITCH:
            # The orchestrator decided this turn is a switch and what it
            # points at; performing it -- validating against the current
            # routing mode via routing_guard, then telling the UI what
            # changed -- is Engine A's, so it lands in the handler that
            # has always done it.
            await self._handle_model_switch_directly(
                result.metadata["model_switch"], conversation_id,
            )
            await self._emit_status(turn_status.IDLE)
            return

        if result.kind == KIND_CLARIFY:
            # A deterministic confirmation, never a model-shaped stream --
            # _send_confirmation emits modelId "system" so the client can
            # tell a fixed prompt from a generated one.
            await self._send_confirmation(result.text)
            await self._emit_status(turn_status.IDLE)
            return

        if result.kind == KIND_TEXT:
            await self._emit_text(result.text, result.model_id)
            await self._emit_status(turn_status.IDLE)
            return

        await self._start_inference_from(result)

    # -----------------------------------------------------
    # Transport-side helpers: everything the orchestrator described but
    # deliberately did not do.
    # -----------------------------------------------------
    @staticmethod
    def _latest_user_text(packet: dict) -> str:
        return next(
            (m.get("content", "") for m in reversed(packet.get("messages", []))
             if m.get("role") == "user"),
            "",
        )

    def _session_snapshot(self) -> SessionState:
        """A read-only view of what this connection is holding."""
        return SessionState(
            mode=self.router.mode_manager.get_mode(),
            explicit_model_override=self.router.mode_manager.get_explicit_model_override(),
            awaiting_weather_location=self._awaiting_weather_location,
            last_turn_was_weather=self._last_turn_was_weather,
            connection_healthy=self._connection_healthy,
            override_model_id=self._override_model_id,
        )

    def _apply_session_updates(self, updates: dict) -> None:
        """Apply what the orchestrator asked for. The only writer of these."""
        if "awaiting_weather_location" in updates:
            self._awaiting_weather_location = updates["awaiting_weather_location"]
        if "last_turn_was_weather" in updates:
            self._last_turn_was_weather = updates["last_turn_was_weather"]
        if "override_model_id" in updates:
            self._override_model_id = updates["override_model_id"]
        if updates.get("explicit_model_override"):
            self.router.mode_manager.set_explicit_model_override(
                updates["explicit_model_override"]
            )

    def _emit_telemetry(self, telemetry: list, conversation_id: str) -> None:
        """Write the log lines the orchestrator described."""
        for record in telemetry or []:
            event = record.get("event")
            if event == "intent_detected":
                log_intent_detected("websocket", record["intent"], conversation_id)
            elif event == "self_query_resolved":
                log_self_query_resolved(
                    "websocket", record["intent"], record["snapshot"], conversation_id
                )
            elif event == "history_policy":
                log_history_policy("websocket", record, conversation_id)
                log_system_prompt_applied("websocket", conversation_id)
            elif event == "tool_routing_decision":
                log_tool_routing_decision("websocket", record["decision"], conversation_id)
            else:
                unified_log("websocket", "INFO", event, {
                    k: v for k, v in record.items()
                    if k != "event" and k != "snapshot"
                } | {"conversation_id": conversation_id})

    async def _emit_status(self, value: str, **fields) -> None:
        """Announce what this turn is doing. Never fails a turn.

        A status packet is decoration: a client that ignores the type
        behaves exactly as it did before, and a send that fails must not
        take the answer down with it.
        """
        try:
            await self._send(turn_status.status_packet(value, **fields))
        except Exception:
            logger.debug("status packet %r not sent", value, exc_info=True)

    async def _emit_text(self, text: str, model_id: str | None) -> None:
        """Send a complete reply as a one-token stream.

        The client already understands this shape -- it is what the
        self-knowledge, weather and search paths have always emitted.
        """
        request_id = id(text)
        await self._send({"type": "stream_start", "modelId": model_id, "requestId": request_id})
        await self._send({"type": "stream_token", "modelId": model_id,
                          "requestId": request_id, "token": text})
        await self._send({"type": "stream_end", "modelId": model_id, "requestId": request_id})

    async def _start_inference_from(self, result) -> None:
        """Stream the provider reply for a turn the orchestrator prepared.

        Signature deliberately unchanged: what the turn ran is already on
        the result, so reading it here beats threading it through as a
        second argument that every caller and test double would have to
        learn about.
        """
        logger.info("Starting inference for model: %s", result.model_id)
        start_time = time.monotonic()
        try:
            await self._stream_inference(
                result.inference_request,
                tool_runs=turn_status.tool_runs_from(result),
            )
        finally:
            elapsed_ms = round((time.monotonic() - start_time) * 1000, 2)
            logger.info("Inference for model %s finished in %s ms", result.model_id, elapsed_ms)
            await self._emit_status(turn_status.IDLE)

    async def _emit_warnings(self, model_cfg: dict, decision) -> None:
        balancer = auto_balancer.get_active_balancer(model_cfg.get("id"))
        events = warning_manager.evaluate_warnings(
            self._warning_session_state,
            model_cfg,
            safety_decision=decision,
            balancer_snapshot=balancer.snapshot() if balancer else None,
            resource_snapshot=self._health_monitor.get_snapshot(),
            thermal=self._health_monitor.get_thermal(),
            cpu_sustained_critical=self._health_monitor.is_cpu_sustained_critical(),
        )
        for event in events:
            await self._send({"type": "warning_event", **event.to_dict()})

    # -----------------------------------------------------
    # Natural-language model/mode switch ("switch to X", "use X",
    # "change to X", "set model to X", "go back to auto mode") —
    # resolved deterministically by
    # conversation_manager.resolve_model_switch_target() before this is
    # ever called, so `resolved` always names a real, validated target.
    # No model is invoked, mirroring _answer_self_query_directly() — the
    # confirmation is a fact about what just happened, not something to
    # generate. The change is permanent: set_active_model() persists to
    # models.json and mode_manager.set_mode()/set_cloud_provider()
    # persist to mode_state.json (see backend/core/mode_manager.py),
    # both surviving reconnects and restarts until changed again.
    # -----------------------------------------------------
    async def _handle_model_switch_directly(self, resolved: dict, conversation_id: str):
        self.conversation_id = conversation_id
        kind = resolved["kind"]

        if kind == "model":
            model_id = resolved["model_id"]
            result = model_manager.set_active_model(model_id)
            if not result.get("ok"):
                logger.warning(f"model_switch_intent — set_active_model failed: {result}")
                await self._send({"type": "error", "message": result.get("reason") or f"Could not switch to '{model_id}'"})
                return

            # Permanent, cross-mode pin — takes precedence over Cloud
            # Mode / Automatic Model Routing mode-based routing (see the resolution logic in
            # _handle_chat_request) until the user switches modes again,
            # which is what clears it (see mode_manager.set_mode()).
            # Routed through routing_guard so a model_id incompatible
            # with the CURRENT routing_mode is rejected here, with a
            # structured error, instead of silently pinning a
            # contradictory override.
            switch = routing_guard.attempt_model_override_switch(self.router.mode_manager, model_id)
            if not switch.ok:
                logger.warning(f"model_switch_intent — routing_guard rejected: {switch.error_code}: {switch.error_message}")
                await self._send(ipc_errors.build_error(
                    ipc_errors.ROUTING_INVARIANT_VIOLATION, switch.error_message, "model_switch_intent",
                ))
                return

            unified_log("websocket", "INFO", "model_switch_intent: model", {"model_id": model_id})
            await self._send({
                "type": "model_set_active_result",
                "payload": {"ok": True, "model_id": model_id, "role": "active"},
            })
            await self._send_confirmation(f"Switched to '{model_id}'. I'll keep using it until you tell me otherwise.")
            return

        if kind == "mode":
            mode = resolved["mode"]
            switch = routing_guard.attempt_mode_switch(self.router.mode_manager, mode)
            if not switch.ok:
                logger.warning(f"model_switch_intent — routing_guard rejected: {switch.error_code}: {switch.error_message}")
                code = ipc_errors.NO_CLOUD_PROVIDER if switch.error_code == "NO_CLOUD_PROVIDER" else ipc_errors.ROUTING_INVARIANT_VIOLATION
                await self._send(ipc_errors.build_error(code, switch.error_message, "model_switch_intent"))
                await self._send_confirmation(switch.error_message or f"Could not switch to {mode} mode.")
                return

            unified_log("websocket", "INFO", "model_switch_intent: mode", {"mode": mode, "cloud_provider": switch.cloud_provider})
            result_payload = {"ok": True, "mode": mode}
            if mode == "cloud":
                result_payload["cloud_provider"] = switch.cloud_provider
            await self._send({"type": "mode_set_result", "payload": result_payload})

            if mode == "automatic":
                text = "Switched to automatic model selection — I'll pick local or cloud per message based on what it needs."
            elif mode == "local":
                text = f"Switched to Local Mode, using {switch.display_name}. I'll stay on local models until you tell me otherwise."
            else:
                text = f"Switched to Cloud Mode, using {switch.display_name} via {switch.cloud_provider}. I'll keep using it until you tell me otherwise."
            await self._send_confirmation(text)
            return

        if kind == "provider":
            provider = resolved["provider"]
            switch = routing_guard.attempt_mode_switch(self.router.mode_manager, "cloud", provider=provider)
            if not switch.ok:
                logger.warning(f"model_switch_intent — routing_guard rejected: {switch.error_code}: {switch.error_message}")
                code = ipc_errors.NO_CLOUD_PROVIDER if switch.error_code == "NO_CLOUD_PROVIDER" else ipc_errors.ROUTING_INVARIANT_VIOLATION
                await self._send(ipc_errors.build_error(code, switch.error_message, "model_switch_intent"))
                await self._send_confirmation(switch.error_message or f"Could not switch to '{provider}'.")
                return

            unified_log("websocket", "INFO", "model_switch_intent: provider", {"provider": switch.cloud_provider})
            await self._send({"type": "mode_set_result", "payload": {"ok": True, "mode": "cloud", "provider": switch.cloud_provider}})
            await self._send_confirmation(f"Switched to '{switch.cloud_provider}' (Cloud Mode), using {switch.display_name}. I'll keep using it until you tell me otherwise.")
            return

        logger.error(f"_handle_model_switch_directly — unknown resolved kind: {resolved!r}")

    # -----------------------------------------------------
    # The four direct-answer helpers that used to live here --
    # _answer_self_query_directly, _answer_tool_query_directly,
    # _answer_weather_intent_directly and _ask_weather_clarification --
    # are gone.
    #
    # Each one made a routing decision (which intent this is), reached
    # for an orchestration primitive (detect_intent, extract_search_query,
    # extract_weather_location, run_search_tool) and wrote its own reply
    # to the socket. That is the whole shape the Turn Orchestrator
    # replaced: the decisions moved to backend/core/turn_orchestrator.py,
    # where REST reaches them too, and the writing stayed here as
    # _emit_text / _send_confirmation.
    #
    # They were already unreachable -- _handle_chat_request stopped
    # calling them when the sequence was extracted -- so this deletion
    # changes no behaviour. It removes the possibility of behaviour: a
    # second, divergent copy of the routing rules that no test covered
    # and that could be called again by mistake. The strings they emitted
    # ("I couldn't search for that...", the "search" and "skr" model
    # sentinels) can now only come from the one path that owns them.
    # -----------------------------------------------------

    # -----------------------------------------------------
    # Handle user override (Proceed Anyway)
    #
    # The user already saw the safety warning for this exact model_id and
    # chose to proceed despite it — this must not immediately re-trigger
    # the same block. allowOverride=True threads down to ModelLoader.
    # load_model(..., allow_override=True), the actual safety bypass (see
    # backend/core/model_loader.py). This is a one-time bypass, not a
    # role change — it deliberately does NOT persist as the active model.
    # -----------------------------------------------------
    async def _handle_model_override(self, packet: dict):
        model_id = packet.get("model_id")
        logger.debug("Model override requested for: %s", model_id)

        if not model_id:
            logger.warning("load_model_override missing model_id.")
            await self._send({
                "type": "error",
                "message": "Missing model_id in load_model_override"
            })
            return

        self._override_model_id = model_id
        unified_log("websocket", "WARNING", "Safety override accepted (proceed anyway)", {
            "model_id": model_id,
        })
        await self._send_confirmation(
            f"Proceeding with '{model_id}' despite the resource warning. Send your next message and I'll use it.",
        )

    # -----------------------------------------------------
    # Handle user choosing a suggested lighter model.
    #
    # Unlike "proceed anyway" above, this IS a role change — the user
    # picked a different model to use going forward, so it must persist
    # (backend.core.model_manager.set_active_model(), the same path the
    # Models page's "Set as Main" button uses) rather than only apply to
    # one throwaway request. That persistence is exactly what was missing
    # before: the switch appeared to work but silently reverted on the
    # very next message because nothing ever updated the registry's
    # active model.
    # -----------------------------------------------------
    async def _handle_switch_to_lighter_model(self, packet: dict):
        model_id = packet.get("model_id")
        logger.debug("Switching to lighter model: %s", model_id)

        if not model_id:
            logger.warning("switch_to_lighter_model missing model_id.")
            await self._send({
                "type": "error",
                "message": "Missing model_id in switch_to_lighter_model"
            })
            return

        result = model_manager.set_active_model(model_id)

        if not result.get("ok"):
            logger.warning(f"switch_to_lighter_model — set_active_model failed: {result}")
            unified_log("websocket", "ERROR", f"switch_to_lighter_model failed: {result.get('reason')}", {
                "model_id": model_id,
            })
            await self._send({
                "type": "error",
                "message": result.get("reason") or f"Could not switch to '{model_id}'",
            })
            return

        # Same packet shape backend/ipc_router.py sends for
        # model_set_active_request — webui/pages/models/models.js already
        # listens for this type and refreshes its list when it's mounted,
        # so the Models page picks up the change with no new packet type.
        await self._send({
            "type": "model_set_active_result",
            "payload": {"ok": True, "model_id": model_id, "role": "active"},
        })

        # One-shot safety bypass for this specific model_id, same
        # mechanism as _handle_model_override — the whole point of
        # picking a suggested lighter model was to resolve the warning,
        # so the very next chat_request for it must not immediately
        # re-trigger a brand new one (which is what "feels like breakage"
        # when baseline system RAM alone is already near/over the safe
        # threshold, since then EVERY model — including the lightest
        # available — would otherwise warn again right away).
        self._override_model_id = model_id

        await self._send_confirmation(
            f"Switched to '{model_id}' as the active model. Send your next message and I'll use it.",
        )

    async def _send_confirmation(self, text: str) -> None:
        """
        A short assistant-style message outside the normal inference
        path — same stream_start/stream_token/stream_end shape a real
        reply uses (see _answer_self_query_directly()), so chat.js
        renders it identically with zero frontend changes, but with no
        model involved. Used where "just tell the user what happened" is
        correct and re-running generation isn't (e.g. there's no original
        message to re-answer once a safety_warning has already replaced
        it in the flow).
        """
        request_id = id(text)
        await self._send({"type": "stream_start", "modelId": "system", "requestId": request_id})
        await self._send({"type": "stream_token", "modelId": "system", "requestId": request_id, "token": text})
        await self._send({"type": "stream_end", "modelId": "system", "requestId": request_id})

    # -----------------------------------------------------
    # Context reset — new chat bubble: rotate conversation_id and let
    # the client know so it can tag subsequent turns with the new one.
    # -----------------------------------------------------
    async def _handle_context_reset(self, packet: dict):
        previous_id = self.conversation_id
        self.conversation_id = packet.get("conversationId") or new_conversation_id()
        log_context_reset("websocket", previous_id, self.conversation_id)

        await self._send({
            "type": "context_reset_ack",
            "conversationId": self.conversation_id,
        })

    # -----------------------------------------------------
    # Start inference (shared by normal + override)
    # -----------------------------------------------------
    # -----------------------------------------------------
    # Streaming wrapper
    # -----------------------------------------------------
    async def _stream_inference(self, request: InferenceRequest, tool_runs=None):
        logger.debug("Starting streaming inference.")

        # streamer.stream() does blocking I/O/CPU work (local GGUF
        # inference, or a synchronous cloud HTTP call) — running it
        # directly here would block this coroutine's event loop for the
        # whole generation, freezing every other connection this server
        # is handling and only flushing packets in one burst at the end
        # once control finally returns to the loop. run_in_executor moves
        # that blocking call onto a worker thread so the loop stays free.
        # The 55 seconds were a model LOAD, before a single token
        # existed. Streaming cannot fix that -- there is nothing to
        # stream while a 12B is being read off disk -- but silence about
        # it is what made it read as a freeze. Saying so costs nothing
        # and is the difference between waiting and wondering.
        await self._announce_model_load(request.model_id)

        loop = asyncio.get_running_loop()
        accumulated_tokens: list[str] = []
        # What the client has actually SEEN, which is not the same as
        # what the model produced: AnswerStream drops scaffolding on the
        # way out. The revision at the end is compared against this, so a
        # turn whose cleaned text matches what was streamed sends no
        # revision at all.
        published: list[str] = []

        # The answer, separated from the scaffolding, before anything is
        # sent. optimize_response below still runs on the raw text, so the
        # log keeps recording what the model actually produced -- what
        # changed is that the client no longer has to see it.
        answer = AnswerStream()
        action_blocks = ActionBlockFilter()
        # The id the client opened its bubble with. The revision and the
        # closing end must carry it, or the client matches neither.
        stream_id: dict = {"value": None}
        writing: dict = {"announced": False, "named": False}

        def send_packet_sync(packet: dict):
            # Called from the executor's worker thread, not the event
            # loop thread — asyncio.create_task() is not thread-safe and
            # would raise (or silently no-op) here. run_coroutine_threadsafe
            # is the thread-safe way to hand a coroutine to a loop running
            # on another thread; it schedules _send() to run as soon as
            # the (now-unblocked) loop gets a turn, which is what makes
            # tokens actually arrive in real time instead of in a burst.
            logger.debug("Streaming packet: %s", packet)

            if packet.get("type") == "stream_start":
                stream_id["value"] = packet.get("requestId")
                asyncio.run_coroutine_threadsafe(
                    self._emit_status(turn_status.WRITING, tool_runs=list(tool_runs or [])),
                    loop,
                )
                if BUFFER_FOR_SUPERVISION:
                    # Nothing reaches the client during generation. The
                    # status above is what says work is happening.
                    return
            elif packet.get("type") == "stream_token":
                raw = packet.get("token", "")
                accumulated_tokens.append(raw)

                if BUFFER_FOR_SUPERVISION:
                    # Accumulated above; held back here. AnswerStream runs
                    # over the whole text at the end instead, exactly as
                    # the REST route already does.
                    return

                # AnswerStream removes scaffolding the model should not
                # have written; this removes the action block, which it
                # SHOULD have written and which the reader should see the
                # effect of rather than the syntax of. Unbuffering put
                # `{"tool": "edit_file"...` on screen one fragment at a
                # time, replaced a second later by "Done:".
                publishable = action_blocks.push(answer.push(raw))

                # The block is hidden, and a big file takes a long time
                # to write. Measured: after an eight-word opening
                # sentence, 93 seconds of nothing while a C# class
                # generated inside a fence the user could not see. Hiding
                # the syntax must not mean hiding that work is happening.
                if action_blocks.suppressed_a_block and not writing["announced"]:
                    writing["announced"] = True
                    asyncio.run_coroutine_threadsafe(
                        self._emit_progress("writing the file"), loop)
                if writing["announced"] and not writing["named"]:
                    path = _path_being_written("".join(accumulated_tokens))
                    if path:
                        writing["named"] = True
                        asyncio.run_coroutine_threadsafe(
                            self._emit_progress(f"writing {path}"), loop)

                if not publishable:
                    # Held back, stripped, or past a terminator. Sending an
                    # empty stream_token would make the client render a
                    # token that carries nothing.
                    return
                published.append(publishable)
                packet = {**packet, "token": publishable}
            elif packet.get("type") == "stream_end":
                if BUFFER_FOR_SUPERVISION:
                    # The supervised text has not been produced yet, so
                    # there is nothing to end. _deliver_supervised sends
                    # start, token and end together once it has.
                    return

                # Anything the filter was still holding when generation
                # stopped -- a final line that never got its newline.
                tail = action_blocks.push(answer.finish()) + action_blocks.finish()
                if tail:
                    published.append(tail)
                    asyncio.run_coroutine_threadsafe(
                        self._send({**packet, "type": "stream_token", "token": tail}), loop,
                    )
                if any(answer.stats.values()):
                    unified_log("websocket", "INFO", "answer_stream filtered model output", {
                        **answer.stats, "conversation_id": self.conversation_id,
                    })
                # Observability only on this path: the tokens above have
                # already been sent (and the client has already rendered
                # them) by the time the full text is available here, so
                # this can't un-send anything — see conversation_manager's
                # RESPONSE OPTIMIZER section for why that's fine (the
                # actual prevention is the provider's stop sequences).
                _, optimize_info = optimize_response("".join(accumulated_tokens))
                log_response_optimized("websocket", optimize_info, self.conversation_id)

                # HELD. The turn is not over: the actions still have to
                # run and the checked text still has to replace what was
                # streamed. _finish_stream sends the revision and then the
                # end, with the SAME requestId the stream opened with.
                #
                # Forwarding it here sent two stream_ends with different
                # ids. The client finalised on the first, cleared its
                # active stream, and then dropped the revision as having
                # nothing to apply to -- so every turn showed the raw
                # answer and none of the cleanup.
                return

            asyncio.run_coroutine_threadsafe(self._send(packet), loop)

        try:
            await loop.run_in_executor(None, self.streamer.stream, request, send_packet_sync)

            # The RAW answer drives the actions, and a rendered copy is
            # what the user reads. They must not be the same string:
            # rendering replaces the action block with a sentence, and
            # parsing that would take the action away from the executor.
            raw_answer = "".join(accumulated_tokens)

            # Actions first, THEN the reply is finished. The tokens are
            # already on screen when streaming; what is still to come is
            # the outcome -- "created hello_world.py" rather than "here
            # is what I would do" -- and that cannot be written until the
            # actions have actually run.
            await self._emit_progress("applying changes")
            report = await self._run_answer_actions(
                self._supervise(raw_answer, request.model_id))

            if BUFFER_FOR_SUPERVISION:
                await self._deliver_supervised(
                    raw_answer, request.model_id, answer, report)
            else:
                await self._finish_stream(
                    raw_answer, "".join(published), request, report,
                    stream_id["value"])

            if report is not None:
                await self._send({"type": "answer_actions", **report})
        except Exception as e:
            # streaming_engine.stream() catches its own errors internally
            # and emits a stream_error packet instead of raising, so this
            # only fires for failures outside that (e.g. StreamingEngine
            # itself misbehaving) — logged under "websocket" since that's
            # the subsystem actually catching it here.
            logger.exception("Inference error: %s", e)
            unified_log("websocket", "ERROR", f"Inference error: {e}", {"model_id": request.model_id})
            await self._send({
                "type": "error",
                "message": f"Inference error: {str(e)}"
            })

    # -----------------------------------------------------
    # Buffered delivery
    # -----------------------------------------------------
    async def _announce_model_load(self, model_id) -> None:
        """Say "loading X" when X is not already in memory.

        Asked of the loader rather than guessed: it holds exactly one
        model at a time and knows which. A warm turn says nothing, which
        is right -- a progress line on every turn is noise, and noise is
        what makes a real one invisible.
        """
        try:
            from backend.core.model_registry import get_model

            # From the provider registry, which returns the same local
            # Provider every call and holds the loader on it. The first
            # version asked ProviderRouter, which has no loader at all --
            # so this returned silently and the load was never announced.
            # A progress line nobody sees is the same as no progress line,
            # and the 39-second blank screen stayed exactly as it was.
            from backend.llm.providers.provider_registry import get_provider

            loader = getattr(get_provider("local"), "loader", None)
            if loader is None or model_id is None:
                return
            if getattr(loader, "active_model_id", None) == model_id:
                return

            name = (get_model(model_id) or {}).get("display_name") or model_id
            await self._emit_progress(f"loading {name}")
        except Exception:  # pragma: no cover - commentary is not worth a turn
            logger.debug("could not announce the model load", exc_info=True)

    async def _emit_progress(self, label: str) -> None:
        """A live line in the chat saying what ARIA is doing right now.

        Separate from the status packet, which drives the one-line
        indicator. This is the running commentary a person reads while
        waiting -- "loading nemo-12b", "applying changes" -- and the
        reason a cold model load stopped looking like a freeze.

        Deliberately NOT a stream_token. Progress is not part of the
        answer: putting it in the token stream would put it in the
        transcript, in the history the next turn reads, and in the text
        actions are parsed from.
        """
        try:
            await self._send({"type": "progress", "label": label,
                              "conversationId": self.conversation_id})
        except Exception:  # pragma: no cover - commentary is not worth a turn
            logger.debug("could not send progress %r", label, exc_info=True)

    async def _finish_stream(self, raw_answer: str, streamed: str,
                             request, report: dict | None,
                             request_id=None) -> None:
        """Replace what was streamed with the checked text, then close.

        This is what buys streaming back without giving up the cleanup.
        The provider's stream_end was held; the actions have now run, so
        the final text can describe what HAPPENED. If it differs from
        what the user watched arrive, a message_revised packet replaces
        the bubble before the stream is closed -- so the transcript ends
        up holding the tidied answer rather than the raw one.

        A turn whose cleaned text matches what was streamed sends no
        revision at all, which is almost every ordinary chat turn.
        """
        final = raw_answer
        try:
            supervised = supervisor_layer.supervise_full(
                raw_answer, request.model_id,
                mode=self.router.mode_manager.get_mode(),
            )
            final = action_render.render_actions_for_reading(
                supervised.text,
                staged=requests_live_execution(self._turn_user_text),
                expected_action=self._turn_expects_action,
                created=(report or {}).get("created") or [],
                problems=(report or {}).get("problems") or {},
                failed=_failed_paths(report),
                new_folders=(report or {}).get("new_folders") or (),
                relocated=(report or {}).get("relocated") or {},
                verification=(report or {}).get("verification"),
            )
        except Exception:
            logger.exception("could not build the revised answer; the streamed text stands")
            final = raw_answer

        if final.strip() and final.strip() != (streamed or "").strip():
            unified_log("websocket", "INFO", "revised the streamed answer", {
                "conversation_id": self.conversation_id,
                "streamed_len": len(streamed or ""), "final_len": len(final),
            })
            await self._send({
                "type": "message_revised",
                "requestId": request_id if request_id is not None else id(raw_answer),
                "modelId": request.model_id,
                "text": final,
            })

        await self._send({
            "type": "stream_end", "modelId": request.model_id,
            "requestId": request_id if request_id is not None else id(raw_answer)})

    async def _deliver_supervised(self, answer_text: str, model_id, answer,
                                  report: dict | None = None) -> str:
        """Supervise the whole reply, then send it.

        This is the trade the buffering makes explicit. Token-by-token
        streaming shows progress immediately and cannot be taken back;
        buffering can clean the text before anyone reads it and costs the
        length of the whole answer in silence. Both are real. The choice
        here is buffering, because a leaked "**assistant:**" or a
        half-repeated paragraph is permanent once it has been read, and
        the status packet keeps the UI honest about the wait.

        Never fails the turn. If supervision raises, the unsupervised
        answer is sent -- silence would be the one outcome worse than
        untidy prose.
        """
        loop = asyncio.get_running_loop()

        try:
            supervised = await loop.run_in_executor(
                None,
                functools.partial(
                    supervisor_layer.supervise_full,
                    answer_text, model_id,
                    mode=self.router.mode_manager.get_mode(),
                ),
            )
            text = supervised.text
            if supervised.changed:
                unified_log("websocket", "INFO", "supervisor revised the reply", {
                    "model_id": model_id, "repairs": supervised.repairs,
                    "supervised": supervised.supervised,
                    "rejected": supervised.rejected,
                    "conversation_id": self.conversation_id,
                })
        except Exception:
            logger.exception("supervision failed; sending the unsupervised answer")
            text = answer_text

        # A proposed action is described, not printed. The model is asked
        # to explain its own block and sometimes does; relying on that is
        # the same mistake as relying on it to spell the tool name right.
        # Measured: the entire reply on screen was the json block and
        # nothing else.
        # `staged` comes from the same function the executor consults,
        # so the sentence and the outcome cannot disagree. Without it the
        # reply said "say yes, do it and I will stage it" about work that
        # had already been staged.
        
        # expected_action: the routing layer already read this turn as
        # file work, so an answer with no action in it is a failure worth
        # naming rather than prose to be shown as though it worked.
        text = action_render.render_actions_for_reading(
            text,
            staged=requests_live_execution(self._turn_user_text),
            expected_action=self._turn_expects_action,
            created=(report or {}).get("created") or [],
            problems=(report or {}).get("problems") or {},
        )

        # The same filter the unbuffered path applies token by token. One
        # pass over the finished text is equivalent, which is what the
        # REST route has always relied on.
        text = (answer.push(text) or "") + (answer.finish() or "")
        if any(answer.stats.values()):
            unified_log("websocket", "INFO", "answer_stream filtered model output", {
                **answer.stats, "conversation_id": self.conversation_id,
            })

        _, optimize_info = optimize_response(text)
        log_response_optimized("websocket", optimize_info, self.conversation_id)

        request_id = id(text)
        await self._send({"type": "stream_start", "modelId": model_id,
                          "requestId": request_id})
        if text:
            await self._send({"type": "stream_token", "modelId": model_id,
                              "requestId": request_id, "token": text})
        await self._send({"type": "stream_end", "modelId": model_id,
                          "requestId": request_id})
        return text

    # -----------------------------------------------------
    # Checking the answer before acting on it
    # -----------------------------------------------------
    def _supervise(self, answer_text: str, model_id: str | None) -> str:
        """Repair a heavier model's answer before its actions are run.

        Deterministic repairs only, and deliberately: the tokens above
        have already been sent, so nothing here can improve what the user
        read. What it CAN still change is what gets executed -- an action
        block is parsed out of this text and run, and a nearly-JSON block
        that parse_actions cannot read is an edit that silently does not
        happen. Repairing it costs no time and no model.

        The model stage is skipped here for that reason: rewriting prose
        the user has already seen would add a second inference to every
        turn to change nothing. The full two-stage pass runs on the REST
        path, where the reply is buffered and the user has not seen it.
        """
        # No role check. The gate used to be needs_supervision(), which
        # was really "is this one of the heavy models" -- so phi-3, the
        # default chat model, had its output repaired by nothing.
        if not answer_text:
            return answer_text

        result = supervisor_layer.repair_deterministically(answer_text)
        if result.repairs:
            unified_log("websocket", "INFO", "supervisor repaired the answer", {
                "model_id": model_id, "repairs": result.repairs,
                "conversation_id": self.conversation_id,
            })
        return result.text

    # -----------------------------------------------------
    # Actions the answer asked for
    # -----------------------------------------------------
    async def _run_answer_actions(self, answer_text: str) -> dict | None:
        """Run the actions in a finished answer, and report what happened.

        Here, and not earlier, because this is the first point at which
        the answer exists. Planning, routing and tool execution all
        happen inside Engine B while the prompt is being assembled; the
        actions are in what the model then wrote, so nothing before
        stream_end has them to run.

        Additive in both directions. run_answer_actions returns None when
        the answer asked for nothing -- which is almost every turn -- and
        this sends no packet at all in that case, so a client that has
        never heard of answer_actions sees exactly the traffic it saw
        before. And it is a dry run unless the USER asked for a live one:
        the consent is read from self._turn_user_text, never from
        answer_text, so a model writing "apply the changes" in its own
        answer has described an intention rather than granted itself one.

        Blocking work -- reading files, writing them, running a test
        suite -- so it goes to the executor for the same reason
        generation does.

        Never fails the turn. The answer has already been streamed and
        the user has already read it; an action that could not run is
        worth reporting, and is not worth turning a delivered answer into
        an error.
        """
        loop = asyncio.get_running_loop()

        def report_progress(message: str) -> None:
            """Called from the executor thread, delivered on the loop.

            Verification is the longest silence in a file turn -- sixteen
            seconds on this project -- and a blank screen for that long
            reads as a hang. The packet cannot be sent from here directly
            because this runs off the event loop, so it is handed back to
            it.
            """
            asyncio.run_coroutine_threadsafe(self._emit_progress(message), loop)

        try:
            report = await loop.run_in_executor(
                None,
                lambda: run_answer_actions(
                    answer_text, self._turn_user_text,
                    on_progress=report_progress),
            )
        except Exception:
            logger.exception("answer actions failed; the answer stands")
            return None

        if report is None:
            return None

        logger.info("answer actions: status=%s dry_run=%s created=%s",
                    report.get("status"), report.get("dry_run"),
                    report.get("created"))
        unified_log("websocket", "INFO", "Answer actions run", {
            "status": report.get("status"),
            "dry_run": report.get("dry_run"),
            "created": report.get("created"),
            "conversation_id": self.conversation_id,
        })
        # Returned rather than sent: the caller delivers the reply first,
        # so the packet arrives after the message it describes.
        return report

    # -----------------------------------------------------
    # Send packet to frontend
    # -----------------------------------------------------
    async def _send(self, packet: dict):
        try:
            logger.debug("Sending packet: %s", packet)
            # stream_token packets are logged individually, at higher detail,
            # by core/streaming_engine.py's per-token callback — logging them
            # again here would double every token in the unified log.
            if packet.get("type") != "stream_token":
                unified_log("websocket", "DEBUG", f"Outgoing packet: {packet.get('type')}", {
                    "packet_type": packet.get("type"),
                })
            await self.websocket.send(json.dumps(packet))
        except Exception as e:
            logger.exception("Failed to send packet: %s", e)
            unified_log("websocket", "ERROR", f"Failed to send packet: {e}")
            # Batch 3 — a failed send is real, direct proof this
            # connection is no longer usable; see _connection_healthy's
            # own comment in __init__ for what consults this.
            self._connection_healthy = False
