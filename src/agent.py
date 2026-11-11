"""Entry point: wires the voice pipeline and picks which agent answers.

The agent itself is glovebox_agent.py. Come here to change models, silence
handling or turn-taking.

  .env.local
    AGENT_PROFILE = glovebox                      (which agent answers)
    MODEL_PRESET  = budget  | quality             (see models.py)
    AGENT_NAME    = take2-agent                   (must match your dispatch rule)
"""

import asyncio
import logging
import os

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    AgentStateChangedEvent,
    JobContext,
    MetricsCollectedEvent,
    STTContextOptions,
    TurnHandlingOptions,
    UserInputTranscribedEvent,
    UserStateChangedEvent,
    cli,
    inference,
    metrics,
    room_io,
)
from livekit.plugins import ai_coustics

from glovebox_agent import GloveboxAgent, agent_for_caller, stt_keyterms
from models import load_models

logger = logging.getLogger("agent")

load_dotenv(".env.local")

AGENTS: dict[str, type[Agent]] = {
    "glovebox": GloveboxAgent,
}

AGENT_NAME = os.getenv("AGENT_NAME", "take2-agent")

# Endpointing: how long to wait after he stops talking before replying. Drivers
# pause mid-number ("five twelve... forty"), so the floor is above the 0.5 s
# default; dynamic mode learns each caller's pace within the range.
ENDPOINTING = {"mode": "dynamic", "min_delay": 0.8, "max_delay": 4.0}

# Silence: after this many quiet seconds the caller counts as "away". Ten, not
# the starter's eight: a driver may be merging or reading a sign.
SILENCE_SECONDS = 10.0
# Check in this many times, then say goodbye and hang up.
MAX_CHECK_INS = 2

# Filler: if the agent is still thinking this long after he stops talking, say a
# short canned line so the line never goes dead. One per turn.
FILLER_DELAY = float(os.getenv("FILLER_DELAY", "0.5"))


async def pick_agent(ctx: JobContext) -> Agent:
    """Glovebox picks by caller ID, so it looks at who's calling first."""
    profile = os.getenv("AGENT_PROFILE", "glovebox").strip().lower()
    if profile not in AGENTS:
        raise ValueError(
            f"Unknown AGENT_PROFILE '{profile}'. Choose one of: {', '.join(AGENTS)}"
        )
    await ctx.connect()
    participant = await ctx.wait_for_participant()
    phone = None
    if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
        phone = participant.attributes.get("sip.phoneNumber") or ""
    agent = agent_for_caller(phone)
    logger.info("caller routed to %s", type(agent).__name__)
    return agent


def handle_silence(session: AgentSession) -> None:
    """Ask "are you still there?" when the caller goes quiet, then hang up."""
    check_ins = 0
    tasks: set[asyncio.Task[None]] = set()

    async def check_in() -> None:
        nonlocal check_ins
        check_ins += 1
        last = check_ins > MAX_CHECK_INS
        # Agents with fixed lines (Glovebox) skip the LLM round trip.
        silence_line = getattr(session.current_agent, "silence_line", None)
        if silence_line:
            await session.say(silence_line(check_ins, last=last)).wait_for_playout()
        elif not last:
            await session.generate_reply(
                instructions="The caller has gone quiet. In a few words, ask if they are still there."
            )
        else:
            await session.generate_reply(
                instructions="The caller is not responding. Say a short, polite goodbye."
            )
        if last:
            session.shutdown()

    @session.on("user_state_changed")
    def _on_user_state(ev: UserStateChangedEvent) -> None:
        nonlocal check_ins
        if ev.new_state == "speaking":
            check_ins = 0
        elif ev.new_state == "away":
            task = asyncio.create_task(check_in())
            tasks.add(task)
            task.add_done_callback(tasks.discard)


def handle_slow_turns(session: AgentSession) -> None:
    """Say a short filler line when the agent thinks for longer than FILLER_DELAY.

    The line comes from the agent's filler_line(transcript), matched to what the
    caller just said. It's kept out of the chat history so the LLM never sees it.
    """
    heard = ""
    pending: asyncio.Task[None] | None = None
    filled = False

    async def fill_after_delay() -> None:
        nonlocal filled
        await asyncio.sleep(FILLER_DELAY)
        filler_line = getattr(session.current_agent, "filler_line", None)
        if session.agent_state == "thinking" and not filled and filler_line:
            filled = True
            logger.info("filler after %.1fs: %s", FILLER_DELAY, heard)
            session.say(filler_line(heard), add_to_chat_ctx=False)

    @session.on("user_input_transcribed")
    def _on_transcript(ev: UserInputTranscribedEvent) -> None:
        nonlocal heard
        if ev.is_final:
            heard = ev.transcript

    @session.on("function_tools_executed")
    def _on_tools_executed(_ev: object) -> None:
        # Never talk over the reply being written from a tool result: in testing a
        # filler at that moment came just before a reply that never arrived.
        nonlocal filled
        filled = True
        if pending and not pending.done():
            pending.cancel()

    @session.on("agent_state_changed")
    def _on_agent_state(ev: AgentStateChangedEvent) -> None:
        nonlocal pending, filled
        if pending and not pending.done():
            pending.cancel()
        if ev.new_state == "listening":
            filled = False  # his next turn gets a new filler
        elif ev.new_state == "thinking":
            pending = asyncio.create_task(fill_after_delay())


def track_latency(session: AgentSession) -> list[float]:
    """Log how long he waits for each reply: end of turn + LLM first token + TTS
    first audio. Returns the running list so the call can be summarized at the end."""
    turns: dict[str, dict[str, float]] = {}
    totals: list[float] = []

    @session.on("metrics_collected")
    def _on_metrics(ev: MetricsCollectedEvent) -> None:
        m = ev.metrics
        sid = getattr(m, "speech_id", None)
        if not sid:
            return
        t = turns.setdefault(sid, {})
        if isinstance(m, metrics.EOUMetrics):
            t["eou"] = m.end_of_utterance_delay
        elif isinstance(m, metrics.LLMMetrics) and "llm" not in t:
            t["llm"] = m.ttft  # first LLM call; tool round trips are covered by filler
        elif isinstance(m, metrics.TTSMetrics) and "tts" not in t:
            t["tts"] = m.ttfb
            if "eou" in t and "llm" in t:
                total = t["eou"] + t["llm"] + t["tts"]
                totals.append(total)
                logger.info(
                    "turn latency %.2fs (end of turn %.2f + llm %.2f + tts %.2f)",
                    total,
                    t["eou"],
                    t["llm"],
                    t["tts"],
                )
            turns.pop(sid, None)

    return totals


server = AgentServer()


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name}
    models = load_models()

    session = AgentSession(
        # Ears: speech-to-text
        stt=models.stt,
        # Bias transcription toward words it would otherwise misspell.
        stt_context_options=STTContextOptions(
            keyterms=stt_keyterms(),
            keyterm_detection={"enabled": True},
        ),
        # Brain: the LLM
        llm=models.llm,
        # Voice: text-to-speech
        tts=models.tts,
        expressive=models.expressive,
        turn_handling=TurnHandlingOptions(
            # Decides when the caller has finished speaking, using both the words
            # and the audio (pitch, pauses). Much better than a fixed silence timer.
            turn_detection=inference.TurnDetector(),
            # Start drafting a reply before the turn is confirmed. Cuts latency.
            preemptive_generation={"enabled": True},
            endpointing=ENDPOINTING,
        ),
        user_away_timeout=SILENCE_SECONDS,
    )

    handle_silence(session)
    handle_slow_turns(session)
    latencies = track_latency(session)

    async def log_usage() -> None:
        # Shows model usage per call so you can see what you're spending.
        logger.info("session usage: %s", session.usage)
        if latencies:
            ordered = sorted(latencies)
            logger.info(
                "call latency over %d turns: median %.2fs, worst %.2fs",
                len(ordered),
                ordered[len(ordered) // 2],
                ordered[-1],
            )

    ctx.add_shutdown_callback(log_usage)

    await session.start(
        agent=await pick_agent(ctx),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                # Removes background noise and other voices before STT hears it.
                noise_cancellation=ai_coustics.audio_enhancement(
                    model=ai_coustics.EnhancerModel.QUAIL_VF_S
                ),
            ),
            # Hang up the phone line when the session ends.
            delete_room_on_close=True,
        ),
    )

    await ctx.connect()  # already connected by pick_agent; kept for other profiles


if __name__ == "__main__":
    cli.run_app(server)
