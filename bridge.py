
# ============================================================
# CONTINUA FORK — pinned from ~/Sagent @ e276913 (2026-09-07)
# This file is Continua's copy of the Sagent organ. Sagent stays
# live and untouched; this fork evolves independently per
# ~/agentwiki/projects/Continua.md. Deviations are tagged [CONTINUA].
# ============================================================

# -*- coding: utf-8 -*-
import os
import io
import yaml
import glob
import html as _html
import json
import logging
import random
import asyncio
import base64
import time
from datetime import datetime
import threading
import httpx
from typing import Dict, List, Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters,
)
from telegram.ext._contexttypes import ContextTypes
from telegram.request import HTTPXRequest
from telegram.error import (
    TimedOut,
    NetworkError,
    RetryAfter,
    BadRequest,
    Forbidden,
    ChatMigrated,
    Conflict,
    EndPointNotFound,
    InvalidToken,
)


# ---------------------------------------------------------------------------
# Resilient Telegram-send helpers
# ---------------------------------------------------------------------------
# python-telegram-bot's built-in network_retry_loop only protects getUpdates
# (long-polling). Outgoing sends — sendMessage, sendChatAction, reply_text —
# have no retry layer. A transient blip (Telegram 502, httpx ReadTimeout,
# intermediate proxy drop) used to surface as a stack trace and the user
# never got their reply. The wrappers below add bounded exponential-backoff
# retries with jitter, honor Telegram's RetryAfter, and explicitly
# distinguish transient from permanent errors.

# Transient: worth retrying. NOTE: In python-telegram-bot 20+, BadRequest
# extends NetworkError (intentional quirk in the v20 rewrite). We must
# therefore list TimedOut explicitly here and NOT include NetworkError,
# otherwise we'd retry malformed-request errors. httpx.TimeoutException
# and httpx.NetworkError are the parent classes covering ConnectTimeout,
# ReadTimeout, WriteTimeout, PoolTimeout, ConnectError, ReadError,
# WriteError, RemoteProtocolError, etc.
_TRANSIENT_TG_ERRORS = (
    TimedOut,
    httpx.TimeoutException,
    httpx.NetworkError,
)

# Permanent: never retry — surface to caller. Includes auth/permission
# failures, malformed-request errors, chat-state changes (migration), and
# endpoint-not-found. BadRequest is listed explicitly even though it
# extends NetworkError in PTB 20+ — retrying a malformed request never
# helps and just amplifies the original problem.
_PERMANENT_TG_ERRORS = (
    BadRequest,
    Forbidden,
    ChatMigrated,
    Conflict,
    EndPointNotFound,
    InvalidToken,
)


async def _send_with_retry(send_fn, *, max_attempts=4, base_delay=1.0,
                           max_delay=30.0, logger_tag="[Send]"):
    """Internal: retry a send-style coroutine factory on transient errors.

    `send_fn` must be a zero-arg callable that returns a coroutine. Each
    retry invokes send_fn() afresh to produce a new coroutine.

    Retries TimedOut, httpx.TimeoutException, and httpx.NetworkError with
    exponential backoff + jitter. Honors RetryAfter (sleeps the requested
    time, then continues). Does NOT retry permanent errors (BadRequest,
    Forbidden, ChatMigrated, Conflict, EndPointNotFound, InvalidToken) —
    those propagate to the caller.

    Returns the awaitable's result on success, None on exhausted retries.
    """
    attempt = 0
    last_exc = None
    while attempt < max_attempts:
        try:
            return await send_fn()
        except RetryAfter as exc:
            # Telegram told us to back off. Sleep the requested time + 0.5s
            # slack, then keep going. Don't count this against our retry
            # budget the same way as a hard failure — it's an instruction.
            sleep_for = float(exc.retry_after) + 0.5
            logger.warning(
                "%s Telegram rate-limited; sleeping %.1fs (attempt %d/%d)",
                logger_tag, sleep_for, attempt + 1, max_attempts,
            )
            await asyncio.sleep(sleep_for)
            attempt += 1
            last_exc = exc
            continue
        except _PERMANENT_TG_ERRORS:
            raise
        except _TRANSIENT_TG_ERRORS as exc:
            last_exc = exc
            attempt += 1
            if attempt >= max_attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            jitter = random.uniform(0, 0.5)
            sleep_for = delay + jitter
            logger.warning(
                "%s Transient send failure: %s — retrying in %.1fs (attempt %d/%d)",
                logger_tag, exc, sleep_for, attempt + 1, max_attempts,
            )
            await asyncio.sleep(sleep_for)
    logger.error(
        "%s Giving up on send after %d attempts. Last error: %s",
        logger_tag, max_attempts, last_exc,
    )
    return None


async def safe_send(bot, chat_id, text, *, max_attempts=4, base_delay=1.0,
                    max_delay=30.0, parse_mode=None,
                    reply_to_message_id=None, logger_tag="[Send]"):
    """Send a message with bounded retry on transient Telegram/httpx errors.

    Returns the sent Message on success, None on exhausted retries.
    Permanent errors (BadRequest, Forbidden, ChatNotFound, etc.) propagate.
    """
    async def _do():
        kwargs = {"chat_id": chat_id, "text": text}
        if parse_mode is not None:
            kwargs["parse_mode"] = parse_mode
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        return await bot.send_message(**kwargs)
    return await _send_with_retry(
        _do, max_attempts=max_attempts, base_delay=base_delay,
        max_delay=max_delay, logger_tag=logger_tag,
    )


async def safe_reply(message, text, *, max_attempts=4, base_delay=1.0,
                     max_delay=30.0, parse_mode=None, logger_tag="[Reply]"):
    """Reply to a message with bounded retry. Same semantics as safe_send."""
    async def _do():
        kwargs = {"text": text}
        if parse_mode is not None:
            kwargs["parse_mode"] = parse_mode
        return await message.reply_text(**kwargs)
    return await _send_with_retry(
        _do, max_attempts=max_attempts, base_delay=base_delay,
        max_delay=max_delay, logger_tag=logger_tag,
    )


async def safe_send_action(bot, chat_id, action, *, max_attempts=1,
                           logger_tag="[ChatAction]"):
    """Send a chat action (typing, upload_photo, etc.) with bounded retry.

    Chat actions are ephemeral — the client only displays the spinner for
    ~5s, so deep retry is wasted. Default max_attempts=1 and short backoff
    match that. Returns True on success, None on exhausted retries.
    """
    async def _do():
        return await bot.send_chat_action(chat_id=chat_id, action=action)
    return await _send_with_retry(
        _do, max_attempts=max_attempts, base_delay=0.5, max_delay=2.0,
        logger_tag=logger_tag,
    )



# Per-user semaphores to serialize concurrent messages for the same history key
_user_semaphores: Dict[str, asyncio.Semaphore] = {}
# W11: protect lazy creation of per-user asyncio.Semaphore. Under
# concurrent_updates > 1 with PTB's int-mode, two coroutines could
# race on the check-then-set; without a lock they'd each create a
# different asyncio.Lock for the same key, and the two locks would
# not serialize (defeating the per-user ordering rule).
_user_semaphores_init_lock = threading.Lock()

# Module-level handle to the SearchEra subprocess so the reaper loop
# (_keep_alive) can monitor and restart it. See fixes.md #18.
_searchera_proc: "asyncio.subprocess.Process | None" = None


def _get_user_semaphore(history_key: str) -> asyncio.Semaphore:
    with _user_semaphores_init_lock:
        if history_key not in _user_semaphores:
            _user_semaphores[history_key] = asyncio.Semaphore(1)
        return _user_semaphores[history_key]

# PyMuPDF for PDF text extraction (already installed in venv)
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

_MAX_PHOTO_BYTES = 10 * 1024 * 1024  # 10 MB cap
_MAX_FILE_CHARS = 300_000


# System Configuration Paths (must come before helpers that use them)
base_dir = os.getenv("SAGENT_BASE", "/tmp/continua")  # [CONTINUA] rebased
BASE_DIR = base_dir
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Dead-letter directory for LLM responses that the agent produced but
# couldn't deliver to Telegram (network outage, chat deleted mid-flight,
# etc.). An admin can grep / replay these by hand. Files are named
# <chat_id>_<unix_ts>.txt with a metadata header + the raw response.
_UNDELIVERED_DIR = os.path.join(LOG_DIR, "undelivered")


def _write_undelivered(chat_id, user_id, response_text,
                       chunks_sent, chunks_total, reason):
    """Persist an undelivered LLM response to disk for later recovery.

    The file intentionally stores the FULL response_text (not just the
    remaining un-sent chunks) so the admin has full context to decide
    what to re-send. Chunks_sent / chunks_total in the header tell them
    how much the user already saw.

    Returns the file path on success, None on failure (logged but not
    raised — we don't want a disk error to crash the message handler).
    """
    try:
        os.makedirs(_UNDELIVERED_DIR, exist_ok=True)
        ts = int(time.time())
        path = os.path.join(_UNDELIVERED_DIR, f"{chat_id}_{ts}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Undelivered response\n")
            f.write(f"# chat_id: {chat_id}\n")
            f.write(f"# user_id: {user_id}\n")
            f.write(f"# timestamp: {ts}\n")
            f.write(f"# chunks_sent: {chunks_sent}/{chunks_total}\n")
            f.write(f"# reason: {reason}\n")
            f.write("# --- FULL RESPONSE ---\n")
            f.write(response_text)
        logger.warning(
            "[Undelivered] Saved %d chars for chat %s to %s "
            "(chunks_sent=%d/%d, reason=%s)",
            len(response_text), chat_id, path,
            chunks_sent, chunks_total, reason,
        )
        return path
    except Exception as e:
        logger.error(
            "[Undelivered] Failed to persist undelivered response for "
            "chat %s: %s", chat_id, e, exc_info=True,
        )
        return None

# Logging Setup — must come BEFORE any function that uses logger
# W07: Custom Formatter that defaults missing fields to "-" so log
# records that don't go through a LoggerAdapter (e.g. bridge
# startup, signal handling) don't KeyError on the format string.
#
# NOTE: a previous version used a LogRecord factory that set
# record.request_id = "-" / record.instance_id = "-" by default.
# That conflicted with LoggerAdapter.process()'s `extra={"request_id": rid}`,
# because makeRecord refuses to overwrite an attribute that's already
# on the record (raises KeyError: "Attempt to overwrite 'request_id' in
# LogRecord"). The getattr-on-read approach below avoids that entirely.
class _SagentFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        if not hasattr(record, "instance_id"):
            record.instance_id = "-"
        return super().format(record)

import logging
import os as _os_dbg
logging.basicConfig(
    # 2026-09-01: SAGENT_LOG_LEVEL=DEBUG traces the update pipeline itself
    # (telegram fetch -> handler dispatch) — used to diagnose silent
    # command drops (the /searchmem-on-residenta incident).
    level=getattr(logging, _os_dbg.environ.get("SAGENT_LOG_LEVEL", "INFO").upper(), logging.INFO),
    # W07: include request_id and instance_id in the format string
    # so every line carries the same identifier that core's
    # LoggerAdapter injects. The custom Formatter above supplies
    # "-" defaults at format time.
    format="%(asctime)s [%(levelname)s] %(name)s [rid=%(request_id)s inst=%(instance_id)s]: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "bridge.log"), encoding="utf-8")
    ]
)
# Replace the default Formatter on every handler with our custom one,
# preserving the format string we passed to basicConfig.
for _h in logging.getLogger().handlers:
    _h.setFormatter(_SagentFormatter(
        "%(asctime)s [%(levelname)s] %(name)s [rid=%(request_id)s inst=%(instance_id)s]: %(message)s"
    ))
# Silence httpx INFO logs: python-telegram-bot's long-poll getUpdates fires every
# 5s per bot and produces ~24 lines/min/bot of pure noise. Our own bridge-level
# logger already covers anything we care about (LLM calls, searchera calls, user
# messages, errors). HTTPX WARNING/ERROR still surfaces if something breaks.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def _extract_photo(update, context) -> List[Dict[str, Any]]:
    """Extract highest-res photo as a base64 image_url content block.

    Returns list of blocks ready for multimodal LLM API, or [] on failure.
    """
    msg = update.message
    raw_photo = getattr(msg, "photo", None)
    if not msg or not raw_photo:
        return []

    # Telegram sends a list/tuple — accept either.
    photo_sizes = list(raw_photo) if isinstance(raw_photo, (list, tuple)) else [raw_photo]
    photo_file = photo_sizes[-1]  # largest size last

    try:
        tg_file = await photo_file.get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(out=buf)
        raw = buf.getvalue()

        if len(raw) > _MAX_PHOTO_BYTES:
            logger.warning(
                "[Photo] Image exceeds cap (%d bytes); still processing.",
                len(raw),
            )

        b64 = base64.b64encode(raw).decode("utf-8")
        mime = "jpeg"  # Telegram photos are JPEG/PNG/WebP - default jpeg for base64 scheme
        if raw.startswith(b'\x89PNG'):
            mime = "png"
        elif raw.startswith(b'GIF8'):
            mime = "gif"
        elif len(raw) >= 12 and raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
            mime = "webp"
        return [{"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}}]

    except Exception as e:
        logger.error("[Photo] Failed to download/encode photo: %s", e, exc_info=True)
        await safe_send(
            context.bot,
            msg.chat_id,
            "Could not process the photo. Please try again.",
            max_attempts=2,
            logger_tag="[Router/photo-err]",
        )
        return []


def _now_iso() -> str:
    """Local-time ISO stamp for history entries (HISTTS, 2026-09-05).

    Local (not UTC) to match the __CURRENT_DATE__ clock the agent already
    sees in its system prompt, so "now" vs "then" comparisons are consistent.
    Rendered by core as [YYYY-MM-DD HH:MM] prefixes at the LLM boundary.
    """
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _flatten_for_disk(history: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Replace multimodal list content with lightweight text for disk JSON persistence."""
    flat = []
    for entry in history:
        content = entry.get("content")
        if isinstance(content, list):
            # Extract any text part and add image placeholders
            text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
            img_labels = ["[Image attached]" for b in content if b.get("type") == "image_url"]
            combined = "\n".join(text_parts + img_labels) or "[Photo attachment]"
            flat_entry = {"role": entry["role"], "content": combined}
            # HISTTS: preserve the timestamp through flattening (previously
            # the rebuilt dict dropped every extra field).
            if entry.get("ts"):
                flat_entry["ts"] = entry["ts"]
            flat.append(flat_entry)
        else:
            flat.append(entry)
    return flat


def _split_message(text: str, max_bytes: int = 3800) -> List[str]:
    """Split a long response into Telegram-compatible message chunks.

    Splits on newlines so each chunk ends cleanly. Falls back to hard split if a single segment exceeds limit.
    Uses UTF-8 byte length (Telegram's real limit is in bytes).
    """
    if len(text.encode("utf-8")) <= max_bytes:
        return [text.strip()] if text.strip() else []

    text = text.strip()

    # Try splitting on blank lines first, then single newlines
    for delim in ["\n\n", "\n"]:
        parts = text.split(delim)
        chunks: List[str] = []
        current = ""
        for part in parts:
            if not current:
                candidate = part
            else:
                candidate = current + delim + part

            if len(candidate.encode("utf-8")) > max_bytes:
                # Current chunk is safe, finalize it
                if current:
                    chunks.append(current)
                current = ""
                # Hard split the oversized segment on word boundaries
                words = part.split()
                acc = ""
                for w in words:
                    test = (acc + " " + w).strip()
                    if len(test.encode("utf-8")) <= max_bytes:
                        acc = test
                    else:
                        if acc:
                            chunks.append(acc)
                        # If a single word exceeds limit, force it
                        acc = w
                current = acc  # will be processed by next iteration or added below
            else:
                current = candidate

        if current:
            chunks.append(current.rstrip())

        # Clean up empty trailing chunks
        while chunks and not chunks[-1].strip():
            chunks.pop()

        # If we got at least one non-empty chunk, use this strategy
        if chunks and any(c.strip() for c in chunks):
            return chunks

    # Absolute fallback: split by byte count (may cut mid-word/mid-UTF8)
    encoded = text.encode("utf-8")
    result: List[str] = []
    start = 0
    while start < len(encoded):
        end = min(start + max_bytes, len(encoded))
        # Align to UTF-8 boundary (don't split mid-sequence)
        while end > start and (encoded[end - 1] & 0xC0) == 0x80:
            end -= 1
        result.append(encoded[start:end].decode("utf-8", errors="replace"))
        start = end

    return result


# Import the updated, multi-tenant core engine
from core import SagentCore

CONFIG_DIR = os.path.join(BASE_DIR, "configs")
HISTORIES_DIR = os.path.join(BASE_DIR, "histories")
DEFAULT_CONFIG_NAME = "sagent_default.yaml"

# Ensure directories exist
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(HISTORIES_DIR, exist_ok=True)


def _clear_stale_locks(instance_path: str) -> None:
    """Per-agent stale qdrant-lock cleanup (2026-08-28: replaced the
    first-agent-only startup sweep, which left every non-first agent's
    stale ``.lock`` file behind after a restart → silent engine-allocation
    failure; see 20:46 spike incident follow-up).

    qdrant local stores mark their directory with a ``.lock`` file and hold
    flock(LOCK_EX|LOCK_NB) on it for the life of the open store
    (qdrant_client/local/qdrant_local.py). The file is NOT removed on clean
    shutdown and carries no PID, so after ANY restart every agent has a
    stale-looking lock file on disk. Deleting a lock while another live
    process holds its flock is unsafe (two openers → local-store
    corruption), so every candidate is probed with the same non-blocking
    exclusive flock: if we acquire it, no live holder exists → stale →
    remove; if we can't, some process has the store open → leave it alone
    (Memory.from_config will then fail LOUDLY with qdrant's standard
    'already accessed by another instance' error rather than corrupt).
    """
    try:
        import portalocker
        from portalocker.exceptions import LockException
    except ImportError:  # pragma: no cover — portalocker ships with qdrant-client
        logger.warning("portalocker unavailable; skipping stale-lock cleanup")
        return
    for pattern in (os.path.join(instance_path, "mem0_db", "**", ".lock"),
                    os.path.join(instance_path, ".mem0", "**", ".lock")):
        for lock_file in glob.glob(pattern, recursive=True):
            try:
                with open(lock_file, "r+") as f:
                    try:
                        portalocker.lock(
                            f,
                            portalocker.LockFlags.EXCLUSIVE
                            | portalocker.LockFlags.NON_BLOCKING,
                        )
                    except LockException:
                        logger.info(
                            "Lock file actively held, leaving in place: %s", lock_file)
                        continue
                    portalocker.unlock(f)
                os.remove(lock_file)
                logger.info("Cleared stale DB lock file: %s", lock_file)
            except Exception as e:
                logger.warning("Could not clear lock file %s: %s", lock_file, e)


class AgentManager:
    """
    Manages the lifecycle of bot configurations across users safely.
    Acts as a single-instance registry per config layout to avoid 
    Qdrant/RocksDB embedded database locks.
    """
    def __init__(self):
        self.instances: Dict[str, SagentCore] = {}
        # [CONTINUA] 2026-09-12: serialize controller creation — the letter
        # turn (get_agent residenta) raced the startup mem0 warmup at 08:44:25,
        # two SagentCore inits collided on the qdrant lock, and the letter's
        # list_my_memories returned "[Tool error: memory store unavailable]"
        # (persona-a's reply to residentb composed without her memory tools).
        self._create_lock = threading.Lock()

    def get_agent(self, config_file: str = None) -> SagentCore:
        target_config = config_file or DEFAULT_CONFIG_NAME
        # 2026-09-01 agentkey fix: cache key is the BASENAME of the config.
        # Callers pass two forms for the same file — the startup mem0 warmup
        # passes full paths (glob over configs/*.yaml) while the router and
        # every handler pass basenames (bot_data["config_file"] = filename).
        # Keying by the raw string produced TWO SagentCore instances per
        # persona: the warm one (with mem0, full-path key, unused by turns)
        # and a router-created one whose Qdrant client lost the flock to the
        # warm one → "already accessed by another instance" → dynamic bypass
        # → live turns ran with memory=None (no injection/extraction). Both
        # callers now converge on one instance. config_path still uses
        # os.path.join, which passes absolute paths through untouched, so
        # full-path callers open the right file. Basename keys are what the
        # P3 consolidator (SAGENT_CONSOLIDATE_SKIP) and the control server
        # already assume.
        instance_key = os.path.basename(target_config)
        
        if instance_key in self.instances:
            return self.instances[instance_key]
        
        with self._create_lock:
            return self._get_agent_locked(target_config, instance_key)

    def _get_agent_locked(self, target_config: str, instance_key: str) -> SagentCore:
        # re-check under the lock: a concurrent creator may have finished
        # while this caller waited (the warmup racing a letter turn is the
        # exact case this lock exists for)
        if instance_key in self.instances:
            return self.instances[instance_key]
        config_path = os.path.join(CONFIG_DIR, target_config)
        logger.info(f"Initializing single-instance agent controller via config: {target_config}")
        
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            
            # Access instance path from app block
            instance_path = config.get('app', {}).get('instance_path') or os.getenv('SAGENT_BASE', '/tmp/continua')  # [CONTINUA] rebased

            # Per-agent stale-lock cleanup on this agent's FIRST init in the
            # process (get_agent early-returns above for known agents, so this
            # runs once per agent). Scoped to this agent's own store trees
            # (mem0_db/ + .mem0/), never other agents'. Each .lock is flock-
            # probed before deletion, so actively-held locks — including
            # stores this same process has open for OTHER agents — are never
            # removed. Supersedes the old first-ever-init-only sweep (2026-08-28).
            _clear_stale_locks(instance_path)

            instance = SagentCore(config)
            self.instances[instance_key] = instance
            return instance
            
        except Exception as e:
            # [CONTINUA] T1 multi-agent (2026-09-11, plan tier 1): the Sagent
            # fallback to DEFAULT_CONFIG_NAME is dead here — sagent_default.yaml
            # does not exist in Continua's configs/, so the fallback re-raised a
            # FileNotFoundError that MASKED the real init error. Worse, falling
            # back to any OTHER persona's engine would answer one resident's
            # users with another persona (identity leak). Fail this agent
            # alone, loudly, with the real cause.
            logger.error(f"Failed to initialize agent for {target_config}: {e}",
                         exc_info=True)
            raise


def _make_history_dir(config_file: str) -> str:
    """Directory per config identity, e.g. ~/Sagent/histories/dreamweaver_yaml/"""
    safe = config_file.replace(".", "_").replace(":", "_")
    return os.path.join(HISTORIES_DIR, safe)


def _history_filepath(config_file: str, user_id: str) -> str:
    """Absolute path to a single-user history JSON file."""
    return os.path.join(_make_history_dir(config_file), f"{user_id}.json")


def _save_history_to_disk(config_file: str, user_id: str, history: list) -> None:
    """Persist a single history entry to disk — atomically.

    Writes to ``<filepath>.tmp`` first, fsyncs, then ``os.replace()``
    for a crash-safe rename. A SIGKILL mid-write leaves the
    original file intact instead of corrupting it. See
    plan/v2/W04-atomic-history-persistence.md.
    """
    filepath = _history_filepath(config_file, user_id)
    tmp_filepath = filepath + ".tmp"
    try:
        os.makedirs(_make_history_dir(config_file), exist_ok=True)
        flat_history = _flatten_for_disk(history)
        with open(tmp_filepath, "w", encoding="utf-8") as f:
            json.dump(flat_history, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_filepath, filepath)
    except Exception as e:
        logger.warning("Failed to save history %s/%s: %s", config_file, user_id, e)
        # Best-effort cleanup of the tmp file on failure
        try:
            os.remove(tmp_filepath)
        except FileNotFoundError:
            pass


def _load_history_from_disk(config_file: str, user_id: str) -> list:
    """Load a history entry from disk; returns empty list if file doesn't exist."""
    filepath = _history_filepath(config_file, user_id)
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        # FileNotFoundError + PermissionError + IsADirectoryError all subclass OSError
        return []


def _load_session_summary(config_file: str, user_id: str) -> str:
    """P1: load the session summary text for this identity ('' if none).

    Delegates to session_memory so the path scheme lives in one place.
    Runs on a worker thread (called via asyncio.to_thread)."""
    import session_memory
    return session_memory.load_summary(_history_filepath(config_file, user_id))


def _make_history_key(config_file: str, user_id: str) -> str:
    """Build a composite key to isolate per-identity conversation history."""
    return f"{config_file}::{user_id}"


async def _extract_file_content(update: Update, context) -> str:
    """Download Telegram document and extract text content.

    Handles .txt files by decoding as UTF-8 and PDFs via PyMuPDF.
    Returns extracted text wrapped in file markers, or empty string on failure.
    """
    if not update.message or not getattr(update.message, "document", None):
        return ""

    doc = update.message.document
    filename = doc.file_name or "unknown_file"
    mime_type = doc.mime_type or ""
    logger.info(f"[FILE] Received document: {filename} (MIME: {mime_type}, size: {doc.file_size or 0} bytes)")

    chat_id = update.effective_chat.id

    # Size guard — Telegram allows up to 20 MB but cap at a reasonable limit for extraction
    MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB hard cap
    file_size = doc.file_size or 0
    if file_size > MAX_FILE_BYTES:
        await safe_send(
            context.bot,
            chat_id,
            f"File '{filename}' exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB limit.",
            max_attempts=2,
            logger_tag="[Router/file-size]",
        )
        return ""

    # Download to memory buffer
    try:
        tg_file = await doc.get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(out=buf)
        raw = buf.getvalue()
    except Exception as e:
        logger.error(f"[FILE] Failed to download {filename}: {e}")
        return ""

    # Extract based on MIME type / file extension
    extracted_text = ""

    if mime_type == "application/pdf":
        # PDF via PyMuPDF
        if fitz is None:
            logger.warning(f"[FILE] Cannot process PDF '{filename}': PyMuPDF not installed.")
            return ""

        try:
            pdf = fitz.open(stream=raw, filetype="pdf")
            pages_text = []
            char_count = 0
            for page_num, page in enumerate(pdf):
                page_text = page.get_text().strip()
                if page_text:
                    pages_text.append(f"--- PAGE {page_num + 1} ---\n{page_text}")
                    char_count += len(page_text)

            pdf.close()

            extracted_text = "\n\n".join(pages_text)
            if not extracted_text:
                logger.warning(f"[FILE] PDF '{filename}' has no extractable text (scanned images?)")
                return ""

        except Exception as e:
            logger.error(f"[FILE] Failed to extract text from PDF '{filename}': {e}")
            return ""

    elif mime_type.startswith("text/") or filename.endswith((".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".xml")):
        # Plain text variants — decode as UTF-8, fallback to latin-1
        try:
            extracted_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning(f"[FILE] '{filename}': UTF-8 failed, falling back to latin-1")
            extracted_text = raw.decode("latin-1", errors="replace")

    else:
        # Unknown MIME type — attempt UTF-8 decode as best effort
        logger.warning(f"[FILE] Unrecognized MIME type '{mime_type}' for '{filename}'; attempting text decode")
        try:
            extracted_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            logger.error(f"[FILE] Cannot extract text from binary file '{filename}' (MIME: {mime_type})")
            return ""

    # Truncate to character budget
    if len(extracted_text) > _MAX_FILE_CHARS:
        extracted_text = extracted_text[:_MAX_FILE_CHARS] + f"\n\n[... rest of file truncated at {_MAX_FILE_CHARS} chars]"
        logger.info(f"[FILE] Extracted {len(extracted_text)} chars from {filename} (truncated)")
    else:
        logger.info(f"[FILE] Extracted {len(extracted_text)} chars from {filename}")

    # Wrap in delimiters so the LLM sees it as a file block
    return f"\n--- FILE: {filename} ---\n{extracted_text}\n--- end of file ---"


# Initialize the global agent manager
agent_manager = AgentManager()

# Thread-safe in-memory store tracking rolling chat history frames per unique (config, user) pair
user_histories: Dict[str, List[Dict[str, Any]]] = {}

# P1 (sagentv3.md Upgrade 1): per-(config, user) session summary text, loaded
# from disk alongside history and refreshed after each persisted turn. The
# on-disk file (<history>.summary.json) is the source of truth; the background
# memory worker updates it, so this dict can lag one turn behind — acceptable.
session_summaries: Dict[str, str] = {}


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start menu sequence with user validation."""
    user_id = str(update.effective_user.id)
    config_file = context.bot_data.get("config_file", DEFAULT_CONFIG_NAME)
    
    try:
        agent = agent_manager.get_agent(config_file)
        allowed = agent.config.get('telegram', {}).get('allowed_users', [])
        if allowed and user_id not in [str(u) for u in allowed]:
            logger.warning(f"Unauthorized user {user_id} blocked at /start command.")
            return
    except Exception:
        pass

    await safe_reply(
        update.message,
        "Welcome to Sagent Framework.\n\nYour session is fully isolated and long-term memory hooks are active.",
        max_attempts=2,
        logger_tag="[start-command/welcome]",
    )


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clears the active conversation window and discards persisted history for this identity."""
    user_id = str(update.effective_user.id)
    config_file = context.bot_data.get("config_file", DEFAULT_CONFIG_NAME)
    key = _make_history_key(config_file, user_id)
    sem = _get_user_semaphore(key)
    async with sem:
        # Fragmerge: any text still inside the debounce window belongs to
        # the conversation being cleared — drop it so it can't fire a turn
        # against the now-empty history.
        _drop_pending_text(key, reason="clear")
        # W02: clear in-memory and on-disk history under the per-user
        # semaphore. This blocks any in-flight turn for this user, so
        # the clear sees a consistent state and the turn sees a
        # consistent empty state. Reply is sent after lock release
        # (see below) so a cleared user can start a new turn
        # immediately without waiting on Telegram send latency.
        user_histories[key] = []
        # P1: drop the session summary too — a cleared session must not leak
        # its folded past into the next one.
        session_summaries.pop(key, None)
        # Disk: atomic removal (see W04 for the atomic write path).
        filepath = _history_filepath(config_file, user_id)
        try:
            os.remove(filepath)
        except FileNotFoundError:
            pass  # nothing on disk to remove
        import session_memory as _session_memory
        _session_memory.clear_summary(filepath)

    await safe_reply(
        update.message,
        "Active conversation context has been reset.",
        max_attempts=2,
        logger_tag="[clear-command]",
    )


# ------------------------------------------------------------------
# /searchmem and /deletemem — memory inspection and management
# ------------------------------------------------------------------
# Both commands are read/write on mem0 long-term memory, scoped to the
# current agent and current user. Each agent's SagentCore wraps its own
# mem0 instance + per-agent Qdrant collection, so cross-agent leakage
# is structurally impossible regardless of how these handlers behave.
# The scope filter (user_id only) is built inside SagentCore so the
# bridge can't accidentally broaden it.

async def _authorized_agent(update, context):
    """Look up the current agent and verify the user is allowed. Returns
    (agent, user_id) or (None, None) if the user isn't on the allow-list
    or the agent can't be resolved. The caller should silently no-op
    in the unauthorized case, matching the behavior of /start and /clear.
    """
    user_id = str(update.effective_user.id)
    config_file = context.bot_data.get("config_file", DEFAULT_CONFIG_NAME)
    try:
        agent = agent_manager.get_agent(config_file)
    except Exception as e:
        logger.warning("[mem-cmd] agent lookup failed for %s: %s", config_file, e)
        return None, None
    try:
        allowed = agent.config.get("telegram", {}).get("allowed_users", []) or []
        if allowed and user_id not in [str(u) for u in allowed]:
            return None, None
    except Exception:
        pass
    return agent, user_id


def _truncate_html_safe(msg: str, limit: int = 3800) -> str:
    """Truncate to Telegram's limit without leaving orphaned tags.

    A naive cut can split a '<s>...</s>' pair and Telegram rejects the
    whole reply (2026-08-28 18:51 incident: 'can't find end tag
    corresponding to start tag "s"' — the user got no answer at all).
    Cut on a line boundary when possible; balance any tag a hard fallback
    cut left open (innermost-first close order: s, i, b).
    """
    if len(msg) <= limit:
        return msg
    notice = "\n\n… <i>(truncated — narrow your query to see more)</i>"
    cut = msg.rfind("\n", 0, limit - len(notice))
    if cut < limit // 2:  # no usable line boundary: hard cut + repair
        cut = limit - len(notice)
    msg = msg[:cut]
    for tag in ("s", "i", "b"):
        _o, _c = msg.count(f"<{tag}>"), msg.count(f"</{tag}>")
        if _o > _c:
            msg += f"</{tag}>" * (_o - _c)
    return msg + notice


async def searchmem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/searchmem <query|all> — list memories for the current user in this agent.

    query omitted  -> usage help
    query == 'all' -> every memory, capped at 50
    otherwise      -> semantic search, top 50
    """
    agent, user_id = await _authorized_agent(update, context)
    if agent is None:
        return

    # W03: serialize against in-flight turns for the same user so a
    # chat extraction can't insert a memory that this search is about
    # to display (or skip). The search itself is read-only; the
    # semaphore is for ordering against the write side.
    config_file = context.bot_data.get("config_file", DEFAULT_CONFIG_NAME)
    key = _make_history_key(config_file, user_id)
    sem = _get_user_semaphore(key)
    async with sem:
        args = context.args or []
        query = " ".join(args).strip() if args else ""
        if not query:
            await safe_reply(
                update.message,
                "<b>Memory search</b>\n"
                "/searchmem &lt;query&gt;  — find memories matching &lt;query&gt;\n"
                "/searchmem all         — list every memory (capped at 50)",
                parse_mode="HTML",
                logger_tag="[searchmem/usage]",
            )
            return

        is_all = query.lower() == "all"
        try:
            if is_all:
                items = await asyncio.to_thread(
                    agent.search_memories, user_id=user_id, query=None, top_k=50,
                )
            else:
                # 2026-08-28: /searchmem now shows WHAT THE BOT GETS — the
                # exact recall_block pipeline the wrapper injects on a turn
                # (focused search -> widen -> entity aug -> superseded filter
                # -> M4 takeaway penalty -> M6 floor/MMR/char budget), plus
                # every candidate the pipeline dropped and why. One source of
                # truth for every memory surface (turns, tool, this command).
                block = await asyncio.to_thread(
                    agent.recall_block, user_id, query, [query], True,
                )
                injected, dropped = block["injected"], block["dropped"]
        except Exception as e:
            logger.warning("[searchmem] call failed: %s", e)
            await safe_reply(update.message, "Memory lookup failed. Please try again.")
            return

        if is_all:
            if not items:
                await safe_reply(update.message, "You have no memories stored in this chat.",
                                 logger_tag="[searchmem/empty]")
                return
            lines = []
            for i, item in enumerate(items, 1):
                text = _html.escape((item.get("memory") or "").strip())
                # P2: show supersession status
                if item.get("superseded_by"):
                    sup_at = (item.get("superseded_at") or "")[:10]
                    tag = "USER_FORGET" if item["superseded_by"] == "USER_FORGET" else "superseded"
                    text = f"<s>{text}</s>  <i>[{tag}{(' ' + sup_at) if sup_at else ''}]</i>"
                lines.append(f"{i}. {text}")
            header = f"Found <b>{len(items)}</b> memor{'ies' if len(items) != 1 else 'y'} for you in this chat"
            body = "\n".join(lines)
        else:
            lines = []
            lines.append("<b>— what the bot gets (injected as-is) —</b>")
            if injected:
                for i, item in enumerate(injected, 1):
                    text = _html.escape((item.get("memory") or "").strip())
                    score = item.get("score")
                    # pipeline items carry a boolean 'superseded' flag; a
                    # superseded row only reaches injection on historical
                    # queries — show it struck, with the reason it's here
                    if item.get("superseded"):
                        text = (f"<s>{text}</s>  <i>[superseded — shown because "
                                f"this is a historical query]</i>")
                    notes = []
                    kind = item.get("kind")
                    if kind == "assistant_takeaway":
                        penalized = not (block["hist"] and getattr(agent, "TAKEAWAY_HISTORICAL_EXEMPT", False))
                        notes.append("takeaway" + (" ×0.75" if penalized else " (historical exempt)"))
                    line = f"{i}. <i>[{float(score or 0):.2f}]</i> {text}"
                    if notes:
                        line += f" <i>({', '.join(_html.escape(n) for n in notes)})</i>"
                    lines.append(line)
            else:
                lines.append("<i>nothing — the bot would inject no memories for this query</i>")
            if dropped:
                lines.append("")
                lines.append(f"<b>— found but not injected ({len(dropped)}) —</b>")
                for item, why in dropped:
                    text = _html.escape((item.get("memory") or "").strip()[:120])
                    lines.append(f"<i>[{float(item.get('score') or 0):.2f}]</i> {text} — <i>{_html.escape(why)}</i>")
            header = f"Memory search matching <i>{_html.escape(str(query))}</i>"
            body = "\n".join(lines)
        footer = (f"\n\nTo delete: /deletemem {'all' if is_all else _html.escape(str(query))} · "
                  "To soft-forget (recall skips, nothing deleted): /forget &lt;query&gt;")

        msg = _truncate_html_safe(header + ":\n\n" + body + footer)
        await safe_reply(update.message, msg, parse_mode="HTML", logger_tag="[searchmem/results]")


async def deletemem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/deletemem <query|all> — show matches and require inline-button confirmation.

    query omitted -> usage help
    query == 'all' -> every memory (one delete_all call after confirm)
    otherwise      -> matches via search (per-id deletes after confirm)
    """
    agent, user_id = await _authorized_agent(update, context)
    if agent is None:
        return

    # W03: same per-user semaphore as /searchmem. The search here
    # builds the confirmation list and stashes ids; the actual
    # delete happens in delete_callback under the same semaphore.
    config_file = context.bot_data.get("config_file", DEFAULT_CONFIG_NAME)
    key = _make_history_key(config_file, user_id)
    sem = _get_user_semaphore(key)
    async with sem:
        args = context.args or []
        query = " ".join(args).strip() if args else ""
        if not query:
            await safe_reply(
                update.message,
                "<b>Memory delete</b>\n"
                "/deletemem &lt;query&gt;  — delete memories matching &lt;query&gt;\n"
                "/deletemem all         — delete every memory (with confirmation)\n\n"
                "<i>Both require inline-button confirmation before anything is removed.</i>",
                parse_mode="HTML",
                logger_tag="[deletemem/usage]",
            )
            return

        is_all = query.lower() == "all"
        try:
            # Pull a larger window for delete confirmation so the user sees the
            # full picture. The cap exists to keep Telegram message body sane.
            items = await asyncio.to_thread(
                agent.search_memories, user_id=user_id,
                query=None if is_all else query, top_k=500,
            )
        except Exception as e:
            logger.warning("[deletemem] search failed: %s", e)
            await safe_reply(update.message, "Memory lookup failed. Please try again.")
            return

        if not items:
            await safe_reply(
                update.message,
                "Nothing to delete — no matches.",
                logger_tag="[deletemem/empty]",
            )
            return

        # Stash the id list (or a marker for "all") in per-user storage so the
        # callback handler can pick it up. Use an opaque token to prevent users
        # from crafting callback_data that affects other users' sessions.
        import secrets
        token = secrets.token_urlsafe(8)
        context.user_data[f"deletemem:{token}"] = {
            "ids": [it.get("id") for it in items if it.get("id")],
            "is_all": is_all,
            "label": query if not is_all else "all",
        }

        header = (
            f"⚠️ About to delete <b>{len(items)}</b> memor"
            f"{'ies' if len(items) != 1 else 'y'}"
        )
        if is_all:
            header += " for you in this chat"
        else:
            header += f" matching <i>{_html.escape(str(query))}</i>"
        header += ".\n\n<i>This cannot be undone.</i>\n\n"

        sample = "\n".join(
            "• " + _html.escape((it.get('memory') or '').strip()[:120])
            for it in items[:5]
        )
        if len(items) > 5:
            sample += f"\n• … and {len(items) - 5} more"
        body = header + sample

        keyboard = [
            [
                InlineKeyboardButton(
                    f"Delete {len(items)}", callback_data=f"deletemem:confirm:{token}",
                ),
                InlineKeyboardButton("Cancel", callback_data="deletemem:cancel"),
            ]
        ]
        await update.message.reply_text(
            body, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML",
        )


async def delete_callback(update, context):
    """Handle [Delete N] / [Cancel] button presses from deletemem_command."""
    query = update.callback_query
    await query.answer()
    if not query.data:
        return

    if query.data == "deletemem:cancel":
        try:
            await query.edit_message_text("Cancelled. No memories deleted.")
        except Exception:
            pass
        return

    if not query.data.startswith("deletemem:confirm:"):
        return
    token = query.data.split(":", 2)[2]
    payload = context.user_data.pop(f"deletemem:{token}", None)
    if not payload:
        try:
            await query.edit_message_text(
                "⚠️ Confirmation expired or already used. Run /deletemem again.",
            )
        except Exception:
            pass
        return

    agent, user_id = await _authorized_agent(update, context)
    if agent is None:
        try:
            await query.edit_message_text("⚠️ Authorization failed; no memory changes made.")
        except Exception:
            pass
        return

    # W03: same per-user semaphore as the chat path. The actual
    # delete runs against the same Mem0 instance the chat extraction
    # uses, so without this a chat turn's extraction can race with
    # a delete and either resurrect a memory (delete-then-extract)
    # or silently lose a memory (extract-then-delete).
    config_file = context.bot_data.get("config_file", DEFAULT_CONFIG_NAME)
    key = _make_history_key(config_file, user_id)
    sem = _get_user_semaphore(key)
    async with sem:
        try:
            if payload["is_all"]:
                deleted = await asyncio.to_thread(
                    agent.delete_all_memories, user_id=user_id,
                )
            else:
                deleted = await asyncio.to_thread(
                    agent.delete_memories, user_id=user_id, memory_ids=payload["ids"],
                )
        except Exception as e:
            logger.warning("[deletemem] delete failed: %s", e)
            try:
                await query.edit_message_text("⚠️ Delete failed. See server logs.")
            except Exception:
                pass
            return

        label = payload.get("label") or ""
        where = " in this chat" if payload["is_all"] else f" matching <i>{label}</i>"
        try:
            await query.edit_message_text(
                f"✓ Deleted <b>{deleted}</b> memor{'ies' if deleted != 1 else 'y'}{where}.",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """P2: /forget <query> — SOFT-invalidate matching memories.

    Unlike /deletemem, nothing is removed: matches get marked superseded so
    recall skips them (except for explicitly historical queries) and
    /searchmem shows them struck-through as [superseded]. Reversible via the
    store; data-safety invariant §7.3 stays intact.
    """
    agent, user_id = await _authorized_agent(update, context)
    if agent is None:
        return

    config_file = context.bot_data.get("config_file", DEFAULT_CONFIG_NAME)
    key = _make_history_key(config_file, user_id)
    sem = _get_user_semaphore(key)
    async with sem:
        args = context.args or []
        query = " ".join(args).strip() if args else ""
        if not query:
            await safe_reply(
                update.message,
                "<b>Memory forget</b>\n"
                "/forget &lt;query&gt; — stop recalling memories matching &lt;query&gt;\n\n"
                "<i>Memories are marked superseded, NOT deleted. They stay "
                "visible in /searchmem and can be asked about historically.</i>",
                parse_mode="HTML",
                logger_tag="[forget/usage]",
            )
            return

        try:
            items = await asyncio.to_thread(
                agent.search_memories, user_id=user_id, query=query, top_k=50,
            )
        except Exception as e:
            logger.warning("[forget] search failed: %s", e)
            await safe_reply(update.message, "Memory lookup failed. Please try again.")
            return

        if not items:
            await safe_reply(update.message, "Nothing matched — no memories forgotten.",
                             logger_tag="[forget/empty]")
            return

        import secrets
        token = secrets.token_urlsafe(8)
        context.user_data[f"forget:{token}"] = {
            "ids": [it.get("id") for it in items if it.get("id")],
            "label": query,
        }

        sample = "\n".join(
            "• " + _html.escape((it.get('memory') or '').strip()[:120])
            + (" <i>[already superseded]</i>" if it.get("superseded_by") else "")
            for it in items[:5]
        )
        if len(items) > 5:
            sample += f"\n• … and {len(items) - 5} more"
        body = (
            f"🧠 About to <b>forget</b> <b>{len(items)}</b> memor"
            f"{'ies' if len(items) != 1 else 'y'} matching <i>{_html.escape(str(query))}</i>.\n\n"
            f"{sample}\n\n"
            "<i>Soft-forget: recall will skip them, but nothing is deleted.</i>"
        )
        keyboard = [[
            InlineKeyboardButton(f"Forget {len(items)}",
                                 callback_data=f"forget:confirm:{token}"),
            InlineKeyboardButton("Cancel", callback_data="forget:cancel"),
        ]]
        await update.message.reply_text(
            body, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML",
        )


async def forget_callback(update, context):
    """Handle [Forget N] / [Cancel] button presses from forget_command."""
    query = update.callback_query
    await query.answer()
    if not query.data:
        return

    if query.data == "forget:cancel":
        try:
            await query.edit_message_text("Cancelled. Nothing forgotten.")
        except Exception:
            pass
        return

    if not query.data.startswith("forget:confirm:"):
        return
    token = query.data.split(":", 2)[2]
    payload = context.user_data.pop(f"forget:{token}", None)
    if not payload:
        try:
            await query.edit_message_text(
                "⚠️ Confirmation expired or already used. Run /forget again.",
            )
        except Exception:
            pass
        return

    agent = agent_manager.get_agent(context.bot_data.get(
        "config_file", DEFAULT_CONFIG_NAME))
    user_id = str(update.effective_user.id)
    config_file = context.bot_data.get("config_file", DEFAULT_CONFIG_NAME)
    key = _make_history_key(config_file, user_id)
    sem = _get_user_semaphore(key)
    async with sem:
        try:
            # Soft path: mark instead of delete (core.forget_memories).
            marked = await asyncio.to_thread(
                agent.forget_memories, user_id, payload["label"],
            )
        except Exception as e:
            logger.warning("[forget] mark failed: %s", e)
            try:
                await query.edit_message_text("⚠️ Forget failed. See server logs.")
            except Exception:
                pass
            return

        try:
            await query.edit_message_text(
                f"✓ Forgot <b>{len(marked)}</b> memor"
                f"{'ies' if len(marked) != 1 else 'y'} matching "
                f"<i>{_html.escape(str(payload.get('label','')))}</i>. They're marked, not deleted — "
                "ask about them in a past-tense way to still see them.",
                parse_mode="HTML",
            )
        except Exception:
            pass

async def _consolidation_loop(interval_s: int):
    """P3: periodic consolidation across all live agents. Skips a cycle while
    the memory queue is busy so background hygiene never competes with
    interactive extraction."""
    import consolidator
    apply_mode = os.getenv("SAGENT_CONSOLIDATE_APPLY", "0") == "1"
    skip = {s.strip() for s in os.getenv("SAGENT_CONSOLIDATE_SKIP", "").split(",") if s.strip()}
    while True:
        await asyncio.sleep(interval_s)
        try:
            if not core._memory_queue.empty():
                logger.info("[P3] memory queue busy; skipping consolidation cycle")
                continue
            for cfg_name, agent in list(agent_manager.instances.items()):
                if agent.memory is None or cfg_name in skip:
                    continue
                collection = agent.mem0_config["vector_store"]["config"]["collection_name"]
                await asyncio.to_thread(
                    consolidator.run_cycle, agent.memory, collection,
                    None, apply_mode, cfg_name,
                )
        except Exception as e:
            logger.warning("[P3] consolidation cycle failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Fragmerge — text-fragment coalescing ("no-frag" merge, ported from FastAI W19)
# ---------------------------------------------------------------------------
# When a user pastes a wall of text, the Telegram CLIENT splits it into
# multiple outbound messages of <=4096 bytes. Without coalescing each fragment
# becomes an independent agent turn: the first 1-2 answers are produced on
# partial knowledge and pollute the history window. Design source of truth:
# ~/fastai/nofragmessages.md (W19); Sagent port keeps the same mechanism with
# the SAGENT_MERGE_WINDOW knob (0 = off = pre-fragmerge behavior).
#
# Mechanism: per-user debounce window. Text-only messages land in
# _pending_texts[history_key]; every arriving fragment resets a timer. When
# the window expires with no new fragment, the buffered texts are joined and
# run through the normal turn machinery exactly once.
#
# CONCURRENCY INVARIANT (critical, do not break): with concurrent_updates > 1
# (SAGENT_UPDATE_CONCURRENCY), each update runs in its own handler task.
# Tasks start in creation order and run their synchronous prefix without
# yielding, so two fragments of one paste execute their prefix in
# update-arrival order. The buffer append below is synchronous and sits
# BEFORE the first await in the router.
# REGRESSION RULE: no `await` may be introduced between handler entry and the
# _buffer_text_fragment call.

# Debounce window in seconds; 0 disables the merge entirely (pre-fragmerge
# behavior — every fragment is its own turn).
_MERGE_WINDOW = float(os.getenv("SAGENT_MERGE_WINDOW", "2.0"))

# Pending text-fragment buffers, keyed by the same composite key as
# user_histories / _user_semaphores (config_file::user_id) so multiple bot
# configs sharing this process stay isolated. Entry schema:
#   {
#     "texts":       List[str],   # fragment texts in arrival order
#     "last_msg":    Message,     # newest fragment (reply anchor)
#     "timer":       TimerHandle, # active call_later handle (cancel on reset)
#     "config_file": str,
#     "user_id":     str,
#     "chat_id":     int,
#     "bot":         Bot,         # for sends from the flush path
#     "created":     float,       # time.monotonic()
#   }
# All access happens on the asyncio loop thread (single-threaded mutation,
# same assumption as user_histories). In-memory only and short-lived
# (<= window + turn time); no eviction needed — a buffer's lifetime is
# bounded by its timer. /clear and shutdown drop them.
_pending_texts: Dict[str, dict] = {}


def _drop_pending_text(history_key: str, reason: str) -> int:
    """Drop a pending fragment buffer WITHOUT running a turn (fragmerge).

    Used by /clear (pending text belongs to the conversation being cleared)
    and by main()'s shutdown path (the window is ~2s; a flush during SIGTERM
    is exactly what the shutdown drain avoids). Returns the number of
    fragments dropped.
    """
    entry = _pending_texts.pop(history_key, None)
    if entry is None:
        return 0
    try:
        entry["timer"].cancel()
    except Exception:
        pass
    logger.info(
        "[Merge] dropped %d buffered fragment(s) for %s (reason=%s)",
        len(entry["texts"]), entry.get("user_id", history_key), reason,
    )
    return len(entry["texts"])


def _schedule_flush(history_key: str, reason: str) -> None:
    """call_later callback — hop back into an asyncio task (fragmerge)."""
    asyncio.create_task(_flush_merged_text(history_key, reason))


async def _flush_merged_text(history_key: str, reason: str) -> None:
    """Pop the pending fragment buffer and run exactly one agent turn.

    Pop-on-flush: the whole buffer is removed synchronously before the first
    real await, so a fragment arriving after the pop starts a fresh buffer
    with its own timer. A stale/late flush (buffer already gone) is a no-op.
    Single-threaded asyncio makes both races safe without extra locking.
    """
    entry = _pending_texts.pop(history_key, None)
    if entry is None:
        return  # late/empty flush — nothing to do
    try:
        entry["timer"].cancel()  # no-op if the timer already fired
    except Exception:
        pass
    # Join with a single newline: a client-side 4096-byte split can land
    # mid-sentence, so "\n" is the most faithful reconstruction of a pasted
    # wall of text (nofragmessages.md §4.6).
    merged = "\n".join(entry["texts"]).strip()
    if not merged:
        logger.info("[Merge] dropping empty buffer for %s (reason=%s)", history_key, reason)
        return
    logger.info(
        "[Merge] flushing %d fragment(s), %d chars for %s (reason=%s)",
        len(entry["texts"]), len(merged), entry["user_id"], reason,
    )
    # Resolve the agent the same way the router does (fragmerge port;
    # _run_agent_turn takes the resolved agent as a parameter).
    try:
        agent = agent_manager.get_agent(entry["config_file"])
    except Exception as e:
        logger.error(f"[Merge] Agent unavailable for flush: {e}", exc_info=True)
        await safe_send(
            entry["bot"], entry["chat_id"],
            "System Error: Unable to instantiate agent instance runtime.",
            max_attempts=2,
            logger_tag=f"[Merge/{entry['user_id']}/agent-fail]",
        )
        return
    await _run_agent_turn(
        bot=entry["bot"],
        chat_id=entry["chat_id"],
        reply_msg=entry["last_msg"],  # reply anchored to the LAST fragment
        agent=agent,
        config_file=entry["config_file"],
        user_id=entry["user_id"],
        history_key=history_key,
        user_input=merged,
        notify_tools=True,
        log_prefix="Merge",
    )


def _buffer_text_fragment(history_key: str, *, msg, text_part: str,
                          bot, config_file: str, user_id: str) -> None:
    """Buffer one text-only fragment, resetting the debounce window.

    MUST be called synchronously (before the handler's first await) so that
    fragments of one paste cannot interleave under concurrent_updates > 1 —
    see the invariant comment on _pending_texts above.
    """
    if _MERGE_WINDOW <= 0:
        return
    entry = _pending_texts.get(history_key)
    loop = asyncio.get_running_loop()
    if entry is not None:
        entry["texts"].append(text_part)
        entry["last_msg"] = msg
        entry["timer"].cancel()
        entry["timer"] = loop.call_later(
            _MERGE_WINDOW, _schedule_flush, history_key, "more-text")
        logger.info(
            "[Merge] fragment %d appended (%d chars) for %s — window reset",
            len(entry["texts"]), len(text_part), user_id,
        )
    else:
        timer = loop.call_later(_MERGE_WINDOW, _schedule_flush, history_key, "window")
        _pending_texts[history_key] = {
            "texts": [text_part],
            "last_msg": msg,
            "timer": timer,
            "config_file": config_file,
            "user_id": user_id,
            "chat_id": msg.chat_id,
            "bot": bot,
            "created": time.monotonic(),
        }
        logger.info(
            "[Merge] buffering fragment 1 (%d chars) for %s (window %.1fs)",
            len(text_part), user_id, _MERGE_WINDOW,
        )


async def _run_agent_turn(
    *,
    bot,
    chat_id: int,
    reply_msg,
    agent,
    config_file: str,
    user_id: str,
    history_key: str,
    user_input,
    notify_tools: bool = True,
    log_prefix: str = "Router",
) -> None:
    """Shared turn machinery: typing -> per-user semaphore -> W01 lazy
    history/summary load -> optimistic append -> admission-wrapped
    generate_response on a worker thread -> persist history + summary
    refresh -> rollback on failure -> chunked send with dead-letter
    recovery.

    Extracted VERBATIM from telegram_message_router (2026-08-30, /tarot N
    command) so the router and tarot_command execute ONE copy of the block
    that produced the 08-04 history-persistence bug and the 08-27
    duplicate-entries bug. Callers only choose the user_input and whether
    tool notifications fire; log tags stay identical via log_prefix.

    2026-09-01 fragmerge port (FastAI W19): the positional update/context
    parameters were replaced by explicit bot/chat_id/reply_msg so the
    fragment-flush path can drive a turn without a synthetic Update.
    reply_msg only needs .reply_text (safe_reply); chat_id is used for all
    non-reply sends and chat actions.
    """
    await safe_send_action(
        bot, chat_id, "typing",
        max_attempts=1,
        logger_tag=f"[{log_prefix}/{user_id}/typing]",
    )

    sem = _get_user_semaphore(history_key)
    async with sem:
        # W01: lazy-load from disk on first message for this (config,
        # user) pair. Now under the per-user semaphore so two
        # concurrent first messages for the same user serialize
        # instead of clobbering each other's history. Disk I/O runs
        # on a worker thread so the event loop doesn't block.
        if history_key not in user_histories:
            user_histories[history_key] = await asyncio.to_thread(
                _load_history_from_disk, config_file, user_id
            )
            # P1: first sight of this user — load any persisted summary and
            # tell the core where future folds should be written. The agent
            # for this config is already resolved above (agent_manager), and
            # _summary_paths is keyed per user so concurrent users don't clash.
            session_summaries[history_key] = await asyncio.to_thread(
                _load_session_summary, config_file, user_id
            )
            agent._summary_paths[user_id] = _history_filepath(config_file, user_id)
        # Capture the main event loop here (we're inside an async function,
        # so it's always running). The tool-notification callback runs on a
        # worker thread (because core's generate_response runs under
        # asyncio.to_thread) and needs this reference to schedule its
        # Telegram send back onto the main loop via
        # asyncio.run_coroutine_threadsafe.
        main_loop = asyncio.get_running_loop()

        # Friendly label per tool, used in the "using X" message so the
        # user sees both the raw function name and a plain-English
        # description. Unknown tool names fall through to a generic
        # phrase so a misconfigured YAML never crashes the bridge.
        tool_friendly = {
            "search_searchera": "searching the web",
            "tarot_draw":       "drawing a tarot card",
        }

        def _tool_notif(tool_name: str) -> None:
            """Sync callback fired by core just before a tool runs.

            We are in a worker thread here (core's generate_response runs
            under asyncio.to_thread). Hop back to the main loop with
            run_coroutine_threadsafe, then fire-and-forget the Telegram
            send — we deliberately do not .result() because a slow
            Telegram send should not block the tool loop.

            The send itself is wrapped in safe_send so a transient blip
            doesn't drop the "Using X" message — but with max_attempts=1
            since these are ephemeral and a slow retry would just delay
            the tool loop.

            Double-guarded: a Telegram send failure must not raise back
            into core's tool loop (core also wraps the callback in
            try/except, but this keeps a Telegram outage from leaving
            a stack trace in the worker thread).
            """
            friendly = tool_friendly.get(tool_name, "running a tool")
            try:
                asyncio.run_coroutine_threadsafe(
                    safe_send(
                        bot,
                        chat_id,
                        f"\U0001F527 Using `{tool_name}` ({friendly}) \u2014 this might take a minute.",
                        max_attempts=1,
                        logger_tag=f"[{log_prefix}/{user_id}/tool-notif/{tool_name}]",
                    ),
                    main_loop,
                )
            except Exception as ex:
                logger.warning("[Router] Tool notification schedule failed for %s: %s", tool_name, ex)

        def _tool_result_cb(
            tool_name: str,
            result_text: str,
            is_error: bool,
            attempt_num: int,
            max_attempts: int,
        ) -> None:
            """Sync callback fired by core after each tool runs.

            On error: send a Telegram message saying the tool failed and
            whether we're going to try again. On success: do nothing —
            the model will produce a final answer shortly, no need to
            spam the user. Same threading pattern as _tool_notif.
            """
            if not is_error:
                return
            # Take the first line of the error so the message stays
            # readable; most tool errors are one-liners. Cap at 120 chars
            # to be safe against long stack-trace-like strings.
            reason = result_text.splitlines()[0] if result_text else "unknown error"
            if len(reason) > 120:
                reason = reason[:117] + "..."
            # "Trying a different angle" wording is only accurate when
            # there's another attempt coming. On the final attempt the
            # fallback message ("I tried to look that up...") will
            # follow, so we say "(final attempt)" instead.
            if attempt_num < max_attempts:
                suffix = f"(attempt {attempt_num} of {max_attempts})"
            else:
                suffix = "(final attempt)"
            try:
                asyncio.run_coroutine_threadsafe(
                    safe_send(
                        bot,
                        chat_id,
                        (
                            f"\U0001F50D `{tool_name}` failed: {reason} \u2014 "
                            f"trying a different angle {suffix}."
                        ),
                        max_attempts=1,
                        logger_tag=f"[{log_prefix}/{user_id}/tool-result/{tool_name}]",
                    ),
                    main_loop,
                )
            except Exception as ex:
                logger.warning("[Router] Tool result callback schedule failed for %s: %s", tool_name, ex)

        # notify_tools=False (the /tarot path): no LLM tool call is
        # expected on that path — the bridge already drew the cards — and
        # core None-guards every callback site, so pass None (no
        # "using X" pings on /tarot turns).
        notif_cb = _tool_notif if notify_tools else None
        result_cb = _tool_result_cb if notify_tools else None

        # [CONTINUA] house ruling 2026-09-12 (Option B): every word she says
        # that is not a tool call is delivered to Alex immediately — her
        # own words, verbatim, split by the standard message splitter.
        def _speech_cb(speech_text: str, iteration: int = 0) -> None:
            try:
                for _part in _split_message(speech_text):
                    asyncio.run_coroutine_threadsafe(
                        safe_send(
                            bot,
                            chat_id,
                            _part,
                            max_attempts=2,
                            logger_tag=f"[{log_prefix}/{user_id}/speech]",
                        ),
                        main_loop,
                    )
            except Exception as ex:
                logger.warning("[Router] speech delivery failed for %s: %s",
                               user_id, ex)

        # --- GENERATION phase (under semaphore to prevent history races) ---
        # Optimistic append: the user message enters the in-memory history
        # before the LLM call. If the turn then fails, the except handler
        # below rolls this append back so a retry/redelivery of the same
        # message doesn't create adjacent duplicate user entries
        # (2026-08-27: two turns each appeared twice after Connection-error
        # failures — one assistant response between two identical user
        # messages).
        # HISTTS (2026-09-05): carry the wall-clock time on the entry.
        # Stored as a structured field; rendered as a [date time] prefix at
        # the LLM boundary (core._render_history_message), never baked into
        # the content string.
        user_entry = {"role": "user", "content": user_input, "ts": _now_iso()}
        user_histories[history_key].append(user_entry)

        # W07: generate a short request ID and pass it through to
        # core.generate_response so every log line in this turn
        # shares the same identifier. Useful for diagnosing
        # concurrent turns from different users.
        import uuid as _uuid
        request_id = _uuid.uuid4().hex[:12]

        # W10: optional process-wide admission limiter. When
        # SAGENT_LLM_ADMISSION_LIMIT > 0, this serializes Sagent's
        # outbound LLM calls to N concurrent. With the default
        # LIMIT=0, this is a no-op. Per Design A in W10 the token
        # is held for the entire turn including tool calls and
        # retry sleeps. Acceptable for the safety-valve use case.
        from admission import make_admission

        try:
            try:
                async with make_admission():
                    response_text, updated_history = await asyncio.to_thread(
                        agent.generate_response,
                        user_id,
                        user_histories[history_key],
                        notif_cb,
                        result_cb,
                        request_id,
                        session_summaries.get(history_key, ""),
                        speech_callback=_speech_cb if notify_tools else None,
                    )
            except ImportError:
                # admission module not available (e.g. unusual deployment);
                # fall through to the un-wrapped call so the bridge still runs.
                response_text, updated_history = await asyncio.to_thread(
                    agent.generate_response,
                    user_id,
                    user_histories[history_key],
                    notif_cb,
                    result_cb,
                    request_id,
                    session_summaries.get(history_key, ""),
                    speech_callback=_speech_cb if notify_tools else None,
                )

            # W03/W04: update the in-memory frame and persist to disk on a
            # worker thread so a slow disk doesn't stall the event loop.
            # MUST run on every successful turn. Previously this block sat
            # inside the `except ImportError` branch, so when the admission
            # module was available the updated history was silently discarded
            # and never written to disk - conversations lived only in RAM and
            # were lost on every bridge restart. The to_thread call stays
            # inside the per-user semaphore so history ordering is preserved
            # by the semaphore, not the thread dispatch.
            user_histories[history_key] = updated_history
            await asyncio.to_thread(
                _save_history_to_disk, config_file, user_id, updated_history
            )
            # P1: refresh the cached summary from disk. The background worker
            # updates the file asynchronously, so this usually returns the
            # previous turn's fold — eventual consistency across turns is the
            # design; never block or fail the turn on it.
            try:
                session_summaries[history_key] = await asyncio.to_thread(
                    _load_session_summary, config_file, user_id
                )
            except Exception as e:
                logger.warning("[P1] summary refresh failed for %s: %s", history_key, e)
        except Exception as e:
            # Roll back the optimistic user-message append if the turn failed
            # before core appended its response. core.generate_response
            # mutates the list in place and raises before its own extend when
            # the LLM call fails, so on failure the last entry is still the
            # user message we just added. Identity check (is user_entry)
            # keeps this a no-op once any response/tool messages exist.
            _hist = user_histories[history_key]
            if _hist and _hist[-1] is user_entry:
                _hist.pop()
            logger.error(f"Inference processing drop encountered for User {user_id}: {e}", exc_info=True)
            # [CONTINUA] house ruling 2026-09-20 (the photo crash): a HARD
            # pre-LLM failure (ContextBudgetExceeded — deterministic, will
            # recur every time) on a RECEIVED message must not vanish into
            # the 2026-09-08 silence ruling: fail loud to the human and
            # record the arrival ("I received something I couldn't process",
            # never a void). Generation/transient failures keep the 09-08
            # ruling — silence is hers.
            if type(e).__name__ == 'ContextBudgetExceeded':
                try:
                    import chronicle as _ch
                    if isinstance(user_input, list):
                        _txt = " ".join(b.get("text", "") for b in user_input
                                        if isinstance(b, dict) and b.get("type") == "text").strip()
                        if any(isinstance(b, dict) and b.get("type") == "image_url" for b in user_input):
                            _txt = (_txt + " [Image attached]").strip()
                    else:
                        _txt = str(user_input or "")
                    _ch.append({"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                                "instance": str(config_file).split('/')[-1].replace('.yaml', ''),
                                "person_id": str(user_id), "role": "user",
                                "content": _txt[:4000] or "[message arrived but could not be processed]",
                                "uid": "arrival-fail-" + datetime.now().strftime("%Y%m%dT%H%M%S")})
                except Exception:
                    logger.warning("[Bridge] arrival capture on hard failure failed (fail-open)",
                                   exc_info=True)
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="(machinery note — not her words: something arrived that could not be "
                             "processed; the failure is logged for the humans. She did not receive it.)")
                except Exception as ex:
                    logger.warning("[%s] fail-loud notice failed: %s", log_prefix, ex)
                return
            try:
                # [CONTINUA] house ruling 2026-09-08: silence is hers — the
                # bridge sends nothing when a turn fails or produces nothing.
                # (The old "An error occurred..." notice answered for her.)
                logger.info("[%s/%s] turn produced no reply — staying silent",
                            log_prefix, user_id)
            except Exception as ex:
                # safe_reply only raises on permanent errors (BadRequest,
                # Forbidden, chat deleted, etc.). Nothing useful to do at
                # that point — log and move on.
                logger.warning(f"[{log_prefix}] Generation-failure fallback reply failed: %s", ex)
            return

        # --- SENDING phase (separate error handling so generation errors don't interfere) ---
        #
        # Each chunk goes through safe_send / safe_reply so transient
        # Telegram / httpx blips retry with backoff instead of dropping
        # the response on the floor. If a chunk ultimately fails, the
        # full original response_text is persisted to the dead-letter
        # directory for manual recovery, and a short notice is sent so
        # the user knows something went wrong (Option A: notice on
        # partial delivery).
        try:
            response_chunks = _split_message(response_text)
            chunks_sent = 0
            for i, chunk in enumerate(response_chunks):
                if i == 0:
                    sent = await safe_reply(
                        reply_msg, chunk,
                        logger_tag=f"[{log_prefix}/{user_id}/chunk-0]",
                    )
                else:
                    sent = await safe_send(
                        bot, chat_id, chunk,
                        logger_tag=f"[{log_prefix}/{user_id}/chunk-{i}]",
                    )
                if sent is None:
                    # Save undelivered text for admin recovery BEFORE the
                    # user-facing notice, so even if the notice itself
                    # fails the LLM's reply is preserved on disk.
                    _write_undelivered(
                        chat_id=chat_id,
                        user_id=user_id,
                        response_text=response_text,
                        chunks_sent=chunks_sent,
                        chunks_total=len(response_chunks),
                        reason=f"send failed at chunk {i}/{len(response_chunks)}",
                    )
                    notice = (
                        f"\u26A0\uFE0F I lost the rest of that message after "
                        f"sending {chunks_sent}/{len(response_chunks)} parts. "
                        f"Try asking again and I'll re-summarize."
                    )
                    await safe_send(
                        bot, chat_id, notice,
                        max_attempts=2,
                        logger_tag=f"[{log_prefix}/{user_id}/chunk-fail-notice]",
                    )
                    break
                chunks_sent += 1
        except Exception as e:
            # safe_send/safe_reply only raise on permanent errors (BadRequest,
            # etc.). This branch is the true last-resort for unexpected
            # non-network exceptions (e.g., a code bug in the loop).
            logger.error(f"Chunk send failure for User {user_id}: {e}", exc_info=True)
            try:
                await safe_reply(
                    reply_msg,
                    "I wasn't able to deliver that response. Please try again — your conversation history is preserved.",
                    max_attempts=2,
                    logger_tag=f"[{log_prefix}/{user_id}/send-fallback]",
                )
            except Exception as ex:
                logger.warning(f"[{log_prefix}] Send-fallback reply failed: %s", ex)


# ---------------------------------------------------------------------------
# /tarot N — multi-card command (tarotv2.md, Flow A — DECIDED 2026-08-30)
# ---------------------------------------------------------------------------
# The TOOL picks the numbers and the cards: the bridge draws N=1-16 cards
# from the tarot service, injects them as a synthetic user turn, and the
# agent performs the reading on exactly those cards. The model never calls
# tarot_draw on this path and never invents cards. Turn machinery is the
# shared _run_agent_turn above (verbatim extraction from the router — one
# copy of the block that produced the 08-04 history-persistence and 08-27
# duplicate-entries bugs).

TAROT_MIN_CARDS = 1
TAROT_MAX_CARDS = 16

# Reading framework injected with every /tarot turn. Adapted from the
# retired "Protocol of Truth" persona block (configs/sagent_default.yaml,
# deleted 2026-08-30): the mandatory-tool-call rule is gone (the bridge
# already drew the cards) and the structure is multi-card. It travels with
# the command, so every persona gets the framework — not just
# sagent_default.
TAROT_READING_FRAMEWORK = (
    "Do a tarot reading for the user using this framework:\n"
    "1. The Overarching Theme — name a clear, resonant theme spanning the "
    "spread in light of the user's situation.\n"
    "2. The Cards — for each card in order, interpret its meaning in the "
    "spread (2–3 distinct viewpoints where useful: traditional archetype vs "
    "counter-intuitive shadow side; immediate external circumstance vs "
    "deeper internal lesson). Challenge the user's mindset or expose blind "
    "spots when the cards support it.\n"
    "3. Grounded Integration — conclude with a warm, perceptive takeaway "
    "tying the spread to what the user needs to hear right now. "
    "Psychological and practical, not mystical or hyperbolic.\n"
    "\n"
    "Deck reference: 78 cards — 1–22 Major Arcana, then Wands, Cups, "
    "Swords, Pentacles (Ace–Ten, Page, Knight, Queen, King)."
)


def _parse_tarot_count(args):
    """Parse /tarot's N. Returns the validated count (1–16) or None when
    the input should get the usage reply (missing, non-integer, out of
    range, or more than one argument)."""
    if not args or len(args) != 1:
        return None
    try:
        n = int(args[0])
    except (TypeError, ValueError):
        return None
    if n < TAROT_MIN_CARDS or n > TAROT_MAX_CARDS:
        return None
    return n


def _tarot_service_draw(n: int) -> list:
    """Draw n cards from the local tarot service. Sync (call under
    asyncio.to_thread); raises on any failure. No seed: /tarot draws are
    meant to be random (same as today's unseeded single-card path)."""
    base_url = os.getenv("TAROT_URL", "http://127.0.0.1:21099")
    with httpx.Client(timeout=10.0) as session:
        resp = session.post(f"{base_url}/draw", json={"count": n})
        resp.raise_for_status()
        payload = resp.json()
    if "error" in payload:
        raise RuntimeError(str(payload["error"]))
    cards = payload.get("cards")
    if not isinstance(cards, list) or len(cards) != n:
        got = len(cards) if isinstance(cards, list) else "no"
        raise RuntimeError(f"tarot service returned {got} cards, expected {n}")
    return cards


def _format_tarot_card(card: dict) -> str:
    """One drawn card, formatted like the tool path renders it:
    'The Moon (Major Arcana)'."""
    name = card.get("card", "?")
    suit = card.get("suit", "unknown")
    return f"{name} (Major Arcana)" if suit == "Major" else name


def _build_tarot_synthetic(n: int, cards: list) -> str:
    """Synthetic user message for a /tarot N turn (Flow A). The [/tarot N]
    prefix doubles as the mem0-extraction marker — a future
    skip-extraction flag can filter on it (tarotv2.md risk 3)."""
    lines = "\n".join(
        f"{i}. {_format_tarot_card(c)}" for i, c in enumerate(cards, 1)
    )
    plural = "s" if n != 1 else ""
    return (
        f"[/tarot {n}] The deck has been shuffled and {n} card{plural} drawn:\n"
        f"{lines}\n\n"
        "The draw has already happened — do NOT call `tarot_draw`. Use "
        "EXACTLY these cards; never substitute, re-draw, or invent cards.\n"
        "\n"
        f"{TAROT_READING_FRAMEWORK}"
    )


async def tarot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tarot <1-16> — draw N cards from the tarot service and have the
    agent read them. Flow A (tarotv2.md): the tool picks the numbers and
    the cards; the model only reads what was drawn."""
    agent, user_id = await _authorized_agent(update, context)
    if agent is None:
        return

    config_file = context.bot_data.get("config_file", DEFAULT_CONFIG_NAME)
    history_key = _make_history_key(config_file, user_id)

    n = _parse_tarot_count(context.args)
    if n is None:
        await safe_reply(
            update.message,
            "<b>Tarot reading</b>\n"
            "/tarot &lt;1-16&gt;  — draw that many cards and do a tarot reading\n"
            "Example: /tarot 3",
            parse_mode="HTML",
            logger_tag="[tarot/usage]",
        )
        return

    # Ack before drawing — replaces the tool path's "drawing a tarot card"
    # notification (no tool call fires on this path).
    await safe_reply(
        update.message,
        f"\U0001F0CF Shuffling the deck\u2026 drawing {n} card{'s' if n != 1 else ''}.",
        max_attempts=1,
        logger_tag=f"[tarot/{user_id}/ack]",
    )

    # Flow A: the bridge draws (localhost stdlib service, ms-fast). Fail
    # fast with a user-visible error BEFORE any history mutation.
    try:
        cards = await asyncio.to_thread(_tarot_service_draw, n)
    except Exception as exc:
        logger.warning("[tarot/%s] draw failed: %s", user_id, exc)
        await safe_reply(
            update.message,
            "\U0001F0CF The tarot service is unavailable right now — please "
            "try again shortly.",
            max_attempts=2,
            logger_tag=f"[tarot/{user_id}/draw-fail]",
        )
        return

    # Standard turn machinery, shared with the message router. No tool
    # notifications: no LLM tool call is expected on this path.
    await _run_agent_turn(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        reply_msg=update.message,
        agent=agent,
        config_file=config_file,
        user_id=user_id,
        history_key=history_key,
        user_input=_build_tarot_synthetic(n, cards),
        notify_tools=False,
        log_prefix="Tarot",
    )



async def telegram_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Asynchronous message router. Enforces user authorization lists and offsets execution
    to background worker threads.

    Accepts text messages, photo uploads, and document uploads. Documents are downloaded in-memory
    and extracted (PDF via PyMuPDF, .txt via UTF-8 decode) before passing to the agent.
    Photos are base64-encoded and sent as multimodal content blocks to the LLM.
    """
    if not update.message:
        return

    # Guard: reject updates that have none of text/caption, photo, or document
    has_text = bool(update.message.text or update.message.caption)
    has_photo = bool(getattr(update.message, "photo", None))
    has_doc = bool(getattr(update.message, "document", None))
    if not has_text and not has_photo and not has_doc:
        return

    user_id = str(update.effective_user.id)
    config_file = context.bot_data.get("config_file", DEFAULT_CONFIG_NAME)
    history_key = _make_history_key(config_file, user_id)

    try:
        agent = agent_manager.get_agent(config_file)
    except Exception as e:
        logger.error(f"Routing system failure. Agent unavailable: {e}")
        await safe_reply(
            update.message,
            "System Error: Unable to instantiate agent instance runtime.",
            max_attempts=2,
            logger_tag=f"[Router/{user_id}/agent-fail]",
        )
        return

    # Security Firewall Enforcer: Check against configuration profile whitelist
    allowed_users = agent.config.get('telegram', {}).get('allowed_users', [])
    if allowed_users and user_id not in [str(uid) for uid in allowed_users]:
        logger.warning(f"Access Denied: User {user_id} is not on the whitelist for config [{config_file}].")
        return

    # Build user input from text + extracted file content + photo
    text_part = update.message.text or update.message.caption or ""

    # --- Fragmerge: coalesce rapid text-only fragments -----------------------
    # IMPORTANT: the buffer append is synchronous and sits BEFORE the first
    # await in this handler (the auth read above is a sync config lookup and
    # text_part extraction is sync). Under concurrent_updates > 1
    # (SAGENT_UPDATE_CONCURRENCY), handler tasks run their sync prefix in
    # update-arrival order, so fragments of one paste can never interleave
    # in the buffer. Do not add an await above this point.
    if _MERGE_WINDOW > 0 and has_text and not has_photo and not has_doc:
        _buffer_text_fragment(
            history_key,
            msg=update.message, text_part=text_part,
            bot=context.bot, config_file=config_file, user_id=user_id,
        )
        # Mask the window latency: keep the typing spinner alive. Chat
        # actions are ephemeral and max_attempts=1, so this cannot break
        # anything — failures are logged and swallowed by safe_send_action.
        await safe_send_action(
            context.bot, update.effective_chat.id, "typing",
            max_attempts=1,
            logger_tag=f"[Merge/{user_id}/typing]",
        )
        return

    # A photo/document (or caption) arrived while text fragments may be
    # pending: flush the pending text FIRST as its own turn so conversation
    # ordering is preserved, then fall through to the normal attachment path.
    # Captions are deliberately NOT buffered (photo path stays untouched).
    # When the window is disabled no buffer can exist and this is a no-op.
    if _MERGE_WINDOW > 0:
        await _flush_merged_text(history_key, reason="attachment-arrival")

    # Handle document notification
    if has_doc and not has_text:
        chat_id = update.effective_chat.id
        doc_obj = getattr(update.message, "document", None)
        doc_name = getattr(doc_obj, "file_name", None) or "unknown"
        await safe_send(
            context.bot, chat_id,
            f"Received {doc_name}. Extracting...",
            max_attempts=1,
            logger_tag=f"[Router/{user_id}/extracting]",
        )

    # Extract file content if a document was sent
    file_content = ""
    if has_doc:
        file_content = await _extract_file_content(update, context)
        if file_content:
            logger.info(f"[FILE] Extraction complete for user {user_id}")

    # Extract photo as multimodal image blocks
    image_blocks = []
    if has_photo:
        logger.info(f"[Photo] Processing photo for user {user_id}")
        image_blocks = await _extract_photo(update, context)

    # Build content: multimodal list (if images) or plain text string
    if image_blocks:
        # Multimodal content — LLM API expects a list of content blocks
        content_blocks: List[Dict[str, Any]] = []
        if text_part:
            content_blocks.append({"type": "text", "text": text_part})
        if file_content:
            content_blocks.append({"type": "text", "text": file_content})
        # Add image blocks
        content_blocks.extend(image_blocks)
        # If no text at all, add a placeholder prompt for the LLM
        if not text_part and not file_content:
            content_blocks.insert(0, {"type": "text", "text": "Please analyze this photo."})
        user_input = content_blocks
    elif file_content and text_part:
        user_input = f"{text_part}\n{file_content}"
    elif file_content:
        user_input = file_content.rstrip("\n")
    else:
        user_input = text_part

    # Turn machinery — typing, per-user semaphore, W01 lazy history load,
    # generation, persistence, rollback, chunked send — lives in
    # _run_agent_turn above (extracted verbatim from this router on
    # 2026-08-30 so the /tarot N command runs the identical block: one copy
    # of the code that produced the 08-04 history-persistence bug and the
    # 08-27 duplicate-entries bug). The router's remaining jobs here are
    # building the user input and keeping tool notifications on.
    await _run_agent_turn(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        reply_msg=update.message,
        agent=agent,
        config_file=config_file,
        user_id=user_id,
        history_key=history_key,
        user_input=user_input,
        notify_tools=True,
    )


async def _run_one_bot(app, filename: str):
    """Run a single Telegram bot with error isolation and clean shutdown.

    This task is the "alive" loop for one bot — it stays alive as long as the
    bot is healthy, and ensures app.stop() runs even on crash. See fixes.md #19.
    """
    try:
        await app.start()
        # dropbump (2026-09-02): pending updates are now DELIVERED by
        # default. The old unconditional drop_pending_updates=True silently
        # discarded every message that arrived while the bridge was down —
        # including the normal restart.sh case, which lost a mid
        # agent-to-agent paste on 2026-09-01 22:42 (the incident that
        # prompted this). Replay storms after long outages remain possible;
        # opt back into the old behavior with SAGENT_DROP_PENDING_UPDATES=1
        # in .env.sagent (systemd EnvironmentFile) when draining a huge
        # backlog on purpose.
        drop_pending = os.getenv("SAGENT_DROP_PENDING_UPDATES", "0") == "1"
        await app.updater.start_polling(drop_pending_updates=drop_pending)
        if drop_pending:
            logger.info(f"Bot pipeline [{filename}] dropping pending updates (SAGENT_DROP_PENDING_UPDATES=1).")
        logger.info(f"Bot pipeline [{filename}] successfully bound and active.")
        # Park this task on a long sleep; the bot's background tasks do the actual work.
        # If the bot's internal tasks fail, this task will see an exception propagate.
        while True:
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        # Propagate so main() can cleanly cancel us on shutdown.
        raise
    except Exception as e:
        logger.error(f"Bot pipeline [{filename}] crashed: {e}", exc_info=True)
    finally:
        # Always try to clean up, even on crash. Best-effort.
        try:
            await app.updater.stop()
        except Exception:
            pass
        try:
            await app.stop()
        except Exception:
            pass
        try:
            await app.shutdown()
        except Exception:
            pass
        logger.info(f"Bot pipeline [{filename}] stopped and cleaned up.")


async def main():
    """
    Discovers configuration profiles using the nested format mappings and binds long-polling.
    Also starts SearchEra API server if available.

    Each bot runs in its own asyncio task via _run_one_bot(); one bot crash does
    not affect the others. The reaper (_keep_alive) supervises SearchEra in
    parallel. See fixes.md #19.
    """
    logger.info("Starting Sagent Bridge Orchestrator Initialization...")

    # [CONTINUA] (2026-09-07, house ruling): SearchEra start PULLED — Sagent
    # owns the SearchEra service on port 21000; this bridge only consumes it
    # via the search_searchera tool (http://localhost:21000/chat).
    # await _start_searchera_server()

    config_pattern = os.path.join(CONFIG_DIR, "*.yaml")
    config_files = glob.glob(config_pattern)

    if not config_files:
        logger.critical(f"No configuration templates found inside: {CONFIG_DIR}. Halting bridge setup.")
        return

    # --- Start SearchEra API server (if available) ---------------------------
    # [CONTINUA] PULLED — Sagent owns SearchEra (port 21000); consume only.
    # await _start_searchera_server()

    # --- Initialize each bot in its own task ---------------------------------
    bot_tasks = []
    for config_path in config_files:
        filename = os.path.basename(config_path)

        # [CONTINUA] house ruling 2026-09-12: a pipeline init that fails during a
        # telegram outage (Bad Gateway/TimedOut during arming) used to SKIP the
        # bot FOREVER — residentb's bot never armed at 18:13 (the outage window) and
        # her telegram polling was down for hours; the designer's replies queued at
        # telegram, unfetched. Retry the arming with backoff (3 attempts), then
        # fail loud (the service restarts and the polls re-arm).
        _attempt = 0
        while True:
            _attempt += 1
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)

                # Map configuration structure changes safely
                telegram_cfg = cfg.get('telegram', {})
                telegram_token = telegram_cfg.get('token') or os.getenv("TELEGRAM_TOKEN")

                if not telegram_token or "YOUR_TELEGRAM" in telegram_token:
                    logger.warning(f"Skipping initialization of [{filename}]: Missing or placeholder 'token'.")
                    continue

                logger.info(f"Spinning up bot pipeline mapped to configuration: {filename}")

                # Custom request with timeouts sized for media uploads. The
                # default HTTPXRequest uses read_timeout=5.0 which is fine
                # for sendMessage and getUpdates, but it is too short for
                # send_photo of a ~1.5 MB PNG — Telegram doesn't always
                # acknowledge within 5s, and the bot raises
                # telegram.error.TimedOut before the upload completes.
                # media_write_timeout=120s covers the upload of large images;
                # read_timeout=30s covers waiting for Telegram's response.
                bot_request = HTTPXRequest(
                    connection_pool_size=8,
                    connect_timeout=10.0,
                    read_timeout=30.0,
                    write_timeout=30.0,
                    pool_timeout=1.0,
                    media_write_timeout=120.0,
                )

                # W11: enable concurrent Telegram update processing. Default
                # is 1; with 2 backend slots we want 2 concurrent handlers
                # per app so two users' messages don't queue at the
                # Telegram layer. Per-user semaphores (W01-W03) still
                # serialize same-user turns. Configurable via
                # SAGENT_UPDATE_CONCURRENCY.
                update_concurrency = int(os.getenv("SAGENT_UPDATE_CONCURRENCY", "1"))
                app = (
                    ApplicationBuilder()
                    .token(telegram_token)
                    .request(bot_request)
                    .concurrent_updates(update_concurrency)
                    .build()
                )
                logger.info(
                    "[Telegram] concurrent_updates=%d (env SAGENT_UPDATE_CONCURRENCY)",
                    update_concurrency,
                )
                app.bot_data["config_file"] = filename

                app.add_handler(CommandHandler("start", start_command))
                app.add_handler(CommandHandler(["clear", "new"], clear_command))
                app.add_handler(CommandHandler("searchmem", searchmem_command))
                app.add_handler(CommandHandler("deletemem", deletemem_command))
                app.add_handler(CallbackQueryHandler(delete_callback, pattern=r"^deletemem:"))
                app.add_handler(CommandHandler("forget", forget_command))
                app.add_handler(CallbackQueryHandler(forget_callback, pattern=r"^forget:"))
                app.add_handler(CommandHandler("tarot", tarot_command))
                app.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND, telegram_message_router))

                await app.initialize()
                # app.start() + polling move into _run_one_bot so a crash in one
                # bot does not block the loop from initializing the next.
                bot_tasks.append(asyncio.create_task(_run_one_bot(app, filename), name=f"bot:{filename}"))
                logger.info(f"Bot pipeline [{filename}] initialized; running as separate task.")
                break

            except Exception as e:
                logger.error(f"Failed to initialize pipeline for config file {filename} (attempt {_attempt}): {e}", exc_info=True)
                if _attempt >= 3:
                    logger.critical(f"[Supervisor] Bot pipeline [{filename}] failed to arm after {_attempt} attempts — it will NOT receive telegram updates until the service restarts.")
                    raise
                await asyncio.sleep(15)

    # --- Supervisor loop -----------------------------------------------------
    # [CONTINUA] (2026-09-07, house ruling): ALL auxiliary service starts are
    # pulled out of Continua — Sagent owns SearchEra (port 21000 + reaper),
    # the control server (nightly writeback endpoint), and the P3
    # consolidation loop. Continua runs ONLY her bot tasks (+ her mem0
    # warmup, which is hers). The forked functions remain defined below but
    # are never started from this process.
    # reaper_task = asyncio.create_task(_keep_alive(), name="searchera-reaper")

    # P3 (sagentv3.md Upgrade 3): idle-time consolidation loop. Disabled by
    # default; enable with SAGENT_CONSOLIDATE_INTERVAL_S (e.g. 3600). Runs
    # DRY-RUN unless SAGENT_CONSOLIDATE_APPLY=1 — reports land in
    # logs/consolidation/ and must be reviewed before trusting apply mode
    # (data-safety invariant §7.2).
    all_tasks = bot_tasks

    # [CONTINUA] wake consumer (2026-09-07): scheduled wake payloads are
    # system-origin turns — they must run IN this process (the mem0 store
    # locks belong to the bridge; a second process would deadlock the store).
    # The wake cadence is the designer's: every 15 min, "doing nothing" is a valid
    # outcome, per-wake action budget bounds what a wake may do.
    async def _wake_consumer_loop(instance: str = "residenta", interval_s: int = 60):
        import glob as _glob
        loop = asyncio.get_running_loop()
        # chunk 8 (memory plan §6g): thread-close trigger — when her
        # conversations go idle ~30 minutes, fold the just-closed exchanges.
        # Bounded (the worker's own limits) and idempotent (durable coverage):
        # a re-trigger after idle finds nothing new to enqueue. Fail-open.
        _idle_close_s = 1800
        _last_scan_mark = 0.0
        while True:
            try:
                config_file = os.path.join(
                    base_dir, "configs", f"{instance}.yaml")
                # [CONTINUA] chunk 3: history/summary paths key on the
                # BASENAME form (the chat path's bot_data["config_file"] is
                # the filename — _make_history_dir replaces ALL dots, so a
                # full path would nest the dir wrongly).
                config_name = f"{instance}.yaml"
                cfg = yaml.safe_load(open(config_file)) or {}
                if ((cfg.get("continua") or {}).get("wake") or {}).get("enabled"):
                    agent = agent_manager.get_agent(f"{instance}.yaml")
                    for path in sorted(_glob.glob(
                            os.path.join(base_dir, "wakes", instance,
                                         "wake_*.json"))):
                        try:
                            with open(path, "r", encoding="utf-8") as f:
                                payload = json.load(f)
                            prompt = payload.get("prompt", "")
                            if not prompt:
                                os.remove(path)
                                continue
                            done_dir = os.path.join(base_dir, "wakes",
                                                    instance, "done")
                            os.makedirs(done_dir, exist_ok=True)
                            done_path = os.path.join(
                                done_dir, os.path.basename(path))
                            os.replace(path, done_path)
                            wake_rid = ("wake-" + os.path.basename(path)
                                        .replace("wake_", "").replace(".json", ""))
                            # [CONTINUA] chunk 3 (memory plan §6g): the wake
                            # thread is PERSISTENT. Load the stable per-resident
                            # wake key (histories/<config>/system-wake.json),
                            # append this wake's prompt, pass the real history —
                            # consecutive wakes see their previous exchanges.
                            # The chat path never touches this key; the wake
                            # consumer owns it exclusively.
                            _wake_loaded = await loop.run_in_executor(
                                None, lambda: _load_history_from_disk(
                                    config_name, "system-wake"))
                            # Summary fold target for evicted exchanges — set
                            # once here so _trim_history_with_evicted's evicted
                            # messages land in system-wake.json.summary.json.
                            agent._summary_paths["system-wake"] = _history_filepath(
                                config_name, "system-wake")
                            _wake_history_in = list(_wake_loaded) + [
                                {"role": "user", "content": prompt}]
                            # [CONTINUA] Option B: her mid-turn words, collected
                            # here and archived after the turn (the actions
                            # file is rewritten post-turn, so the callback
                            # only collects).
                            _speeches = []

                            def _wake_speech(speech_text: str,
                                             iteration: int = 0) -> None:
                                _speeches.append(str(speech_text))

                            _speeches = []

                            def _wake_speech(speech_text: str,
                                             iteration: int = 0) -> None:
                                _speeches.append(str(speech_text))

                            text, wake_hist = await loop.run_in_executor(
                                None,
                                lambda: agent.generate_response(
                                    "system-wake",
                                    _wake_history_in,
                                    request_id=wake_rid,
                                    max_tool_iterations=int(
                                        os.getenv("CONTINUA_WAKE_MAX_ITERATIONS",
                                                  "8")),
                                    max_tool_actions=int(
                                        (payload.get("budget") or {}).get(
                                            "max_actions", 3)),
                                    # [CONTINUA] Option B: her mid-turn words
                                    # are archived (kind: speech) and join the
                                    # response file — no chat to deliver to.
                                    speech_callback=_wake_speech),
                            )
                            # [CONTINUA] the designer ask 2026-09-08: persist the wake's
                            # tool actions — the chronicle records only the
                            # final reply, so saves/bookmarks/mail-checks lived
                            # only in the journal. The returned history carries
                            # the full turn chain: extract and archive it.
                            # [CONTINUA] chunk 3: the same chain is the wake
                            # thread's new history — trimmed via the agent's
                            # pair-aware trim (evictions fold into the wake
                            # summary, never silently dropped) and persisted
                            # atomically to the stable per-resident wake key.
                            try:
                                _kept, _evicted = agent._trim_history_with_evicted(
                                    list(wake_hist or []),
                                    # §4c sizing: the WAKE thread's window is
                                    # its own 10,000 chars (the plan's number;
                                    # the chat window is separate at 25,000).
                                    target_chars=int(os.getenv(
                                        "CONTINUA_WAKE_WINDOW_CHARS", "10000")))
                                if _evicted:
                                    try:
                                        import session_memory as _sm
                                        _summary_path = agent._summary_paths.get(
                                            "system-wake")
                                        if _summary_path:
                                            _sm.maybe_update_summary(
                                                _summary_path, _kept,
                                                evicted_messages=_evicted)
                                    except Exception as _se:
                                        logger.warning(
                                            "[Wake] summary fold failed (%s): %s",
                                            wake_rid, _se)
                                await loop.run_in_executor(
                                    None, lambda: _save_history_to_disk(
                                        config_name, "system-wake", _kept))
                            except Exception as _pe:
                                logger.warning(
                                    "[Wake] history persistence failed (%s): %s",
                                    wake_rid, _pe)
                            try:
                                import re as _re
                                _actions = []
                                for _m in (wake_hist or [])[1:]:
                                    _role = _m.get("role")
                                    if _role == "assistant":
                                        for _tc in (_m.get("tool_calls") or []):
                                            _fn = ((_tc.get("function") or {})
                                                   .get("name", "?"))
                                            _actions.append({
                                                "kind": "native", "fn": _fn})
                                        for _fn in _re.findall(
                                                r"<function>(\w+)</function>",
                                                _m.get("content") or ""):
                                            _blk = _m["content"]
                                            _i = _blk.find("<function>" + _fn)
                                            _params = dict(_re.findall(
                                                r'<parameter name="([^"]+)">'
                                                r'(.*?)</parameter>',
                                                _blk[_i:_i + 800], _re.S))
                                            _actions.append({
                                                "kind": "text", "fn": _fn,
                                                "params": {
                                                    k: v[:200]
                                                    for k, v in _params.items()}})
                                    elif _role == "tool":
                                        _rc = _m.get("content") or ""
                                        _actions.append({
                                            "kind": "result",
                                            "ok": not _rc.startswith("[Tool error"),
                                            "preview": _rc[:150]})
                                if _actions:
                                    with open(done_path.replace(
                                            ".json", ".actions.jsonl"),
                                            "w", encoding="utf-8") as _af:
                                        # [CONTINUA] Option B: her mid-turn
                                        # words lead the actions archive.
                                        for _sp in _speeches:
                                            _af.write(json.dumps({
                                                "wake": wake_rid,
                                                "ts": datetime.now().isoformat(
                                                    timespec="seconds"),
                                                "kind": "speech",
                                                "text": _sp[:2000]}) + "\n")
                                        for _a in _actions:
                                            _af.write(json.dumps({
                                                "wake": wake_rid,
                                                "ts": datetime.now().isoformat(
                                                    timespec="seconds"),
                                                **_a}) + "\n")
                            except Exception:
                                logger.warning("[Wake] actions archive failed "
                                               "(fail-open)", exc_info=True)
                            with open(done_path.replace(".json",
                                                        ".response.txt"),
                                      "w", encoding="utf-8") as f:
                                f.write((text or "(no text output)") + "\n")
                            # [CONTINUA] Option B: her mid-turn words join the
                            # response file — the full turn as she said it.
                            if _speeches:
                                with open(done_path.replace(
                                        ".json", ".response.txt"),
                                        "a", encoding="utf-8") as f:
                                    f.write("\n— said mid-turn —\n")
                                    for _sp in _speeches:
                                        f.write("- " + _sp[:1200] + "\n")
                            logger.info("[Wake] consumed %s → response %d chars",
                                        os.path.basename(path), len(text or ""))
                            # Recollections worker is bounded and coalesces
                            # durable chronicle sources; no narrator re-roll.
                            import recollections as _rec
                            _rec.request_shadow(instance)
                            _last_scan_mark = time.time()
                        except Exception as e:
                            logger.warning("[Wake] consume failed (fail-open): %s", e)
            except Exception as e:
                logger.warning("[Wake] consumer loop error: %s", e)
            # chunk 8: thread-close consolidation trigger — conversations idle
            # ~30 min fold their just-closed exchanges into the recollection
            # path, even with no chat turn to piggyback on. Idempotent: the
            # trigger fires once per new chronicle write, and the durable
            # coverage makes the scan a no-op when everything is enqueued.
            try:
                import recollections as _rec
                _newest = 0.0
                _chron_dir = os.path.join(base_dir, "chronicle", instance)
                if os.path.isdir(_chron_dir):
                    for _root_d, _dirs, _files in os.walk(_chron_dir):
                        for _f in _files:
                            if _f.endswith('.jsonl'):
                                _mt = os.path.getmtime(os.path.join(_root_d, _f))
                                _newest = max(_newest, _mt)
                if (_newest and time.time() - _newest >= _idle_close_s
                        and _newest > _last_scan_mark):
                    _rec.request_shadow(instance)
                    _last_scan_mark = _newest
            except Exception as e:
                logger.warning("[Wake] thread-close trigger failed (fail-open): %s", e)
            await asyncio.sleep(interval_s)

    # [CONTINUA] T2 multi-agent (2026-09-11): one consumer per wake-enabled
    # resident, discovered at startup (boot-load convention — a dropped YAML
    # needs a restart anyway, so discovery at boot is the contract; the
    # per-iteration config re-read inside the loop keeps a later disable
    # honored without code changes). The old single hardcoded residenta loop was
    # the reason a second resident's wakes would never be consumed.
    import wake as _wake
    _wake_instances = _wake.enabled_instances()
    if _wake_instances:
        for _wi in _wake_instances:
            all_tasks.append(asyncio.create_task(
                _wake_consumer_loop(_wi, 60),
                name=f"continua-wake-consumer:{_wi}"))
        logger.info("[Wake] consumers armed for: %s", ", ".join(_wake_instances))
    else:
        logger.info("[Wake] no wake-enabled residents; consumer not armed")
    # [CONTINUA] control server PULLED — Sagent owns the single-writer
    # endpoint for nightly consolidation writeback:
    # import control_server as _ctl
    # _srv = await _ctl.ControlServer(agent_manager).start()
    # if _srv is not None:
    #     all_tasks.append(asyncio.create_task(_srv.serve_forever(), name="control-server"))
    #     logger.info("[Control] task added to supervised set")
    # Mem0 warmup (2026-09-01): construct every agent and allocate its mem0
    # engine in a background thread at startup. Without this, the FIRST user
    # command after a restart lands in the cold lazy-allocation window and
    # silently stalls for the allocation's duration (the /searchmem incident:
    # the command vanished while a second process contended the store during
    # allocation). Serialized — the per-agent mem0 module rebind (see
    # core.py _init_memory) is only safe without interleaving.
    def _warm_agents(cf_list):
        # §7.6 producer shutdown (2026-09-19): when a resident's mem0
        # producer is off and its injection is retired, the engine has no
        # caller left at boot — skip the allocation (cold) instead of
        # paying the engine build. A lazy allocation still fires if a tool
        # that truly needs the store is used.
        for cf in cf_list:
            try:
                agent = agent_manager.get_agent(cf)
                if hasattr(agent, "_init_memory") and getattr(agent, "_mem0_producer", True):
                    t0 = time.time()
                    agent._init_memory()
                    logger.info("[warmup] %s mem0 engine warm (%.1fs)", cf, time.time() - t0)
                else:
                    logger.info("[warmup] %s mem0 engine COLD (producer off / retired injection)", cf)
            except Exception:
                logger.warning("[warmup] %s warmup failed", cf, exc_info=True)
        try:
            with open(os.path.join(LOG_DIR, "mem0_warm.done"), "w") as f:
                f.write(time.strftime("%F %T"))
        except OSError:
            pass
    threading.Thread(target=_warm_agents, args=(config_files,),
                     name="sagent-mem0-warmup", daemon=True).start()

    _consolidate_interval = int(os.getenv("SAGENT_CONSOLIDATE_INTERVAL_S", "0"))
    if _consolidate_interval > 0:
        # [CONTINUA] P3 consolidator PULLED — Sagent owns consolidation.
        pass

    try:
        await asyncio.gather(*all_tasks)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown signal received; cancelling all tasks...")
        for t in all_tasks:
            t.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
        raise
    finally:
        # Fragmerge: drop any text fragments still inside the debounce
        # window. A flush during SIGTERM would fire exactly the turn the
        # shutdown drain is trying to avoid; log what we discarded.
        try:
            if _pending_texts:
                n_bufs = len(_pending_texts)
                dropped = sum(
                    _drop_pending_text(k, reason="shutdown")
                    for k in list(_pending_texts)
                )
                logger.info(
                    "[Merge] shutdown dropped %d fragment(s) in %d buffer(s)",
                    dropped, n_bufs,
                )
        except Exception:
            pass
        # W13: clean up on any exit path (KeyboardInterrupt, SystemExit,
        # or normal completion). Best-effort; failures here don't
        # block the process exit.
        try:
            for core in list(agent_manager.instances.values()):
                try:
                    core.aclose()
                except Exception as e:
                    logger.warning(f"Error closing SagentCore: {e}")
        except Exception as e:
            logger.warning(f"Error during SagentCore cleanup: {e}")
        try:
            from core import _stop_memory_workers
            _stop_memory_workers(timeout_s=2.0)
        except Exception as e:
            logger.warning(f"Error stopping Mem0 workers: {e}")


async def _launch_searchera():
    """Launch the SearchEra subprocess; return the process handle.

    Raises FileNotFoundError if the server script is missing, or any
    other exception on launch failure. Used by both initial startup
    and the reaper's restart loop in `_keep_alive()`. See fixes.md #18.
    """
    SERVER_SCRIPT = os.path.join(
        BASE_DIR, "..", "searchera", "server.py"
    )
    SERVER_SCRIPT = os.path.normpath(SERVER_SCRIPT)

    VENV_PYTHON = os.path.join(
        os.path.dirname(SERVER_SCRIPT), ".venv", "bin", "python"
    )

    if not os.path.isfile(SERVER_SCRIPT):
        raise FileNotFoundError(f"SearchEra server script not found at {SERVER_SCRIPT}")

    # Use the venv python so uvicorn/fastapi are available
    venv_python = VENV_PYTHON if os.path.isfile(VENV_PYTHON) else "python3"

    # CRITICAL: Strip PYTHONPATH from environment. The Hermes sandbox injects
    # hermes-agent's Python 3.11 site-packages into PYTHONPATH, which causes
    # pydantic_core ABI mismatches (ModuleNotFoundError) when the searchera venv
    # (Python 3.12) tries to import its own pydantic from a different ABI.
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["SEARCHERA_HOST"] = "0.0.0.0"
    env["SEARCHERA_PORT"] = "21000"

    # SearchEra performs its own durable logging via the SEARCHERA_LOG_FILE
    # handler configured in server.py, so we discard stdout/stderr here
    # rather than capturing them a second time. Using DEVNULL (not PIPE)
    # avoids the OS pipe-buffer deadlock noted in fixes.md #22.
    return await asyncio.create_subprocess_exec(
        venv_python, SERVER_SCRIPT,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )


async def _start_searchera_server():
    """Start SearchEra FastAPI server as a background process on localhost:21000 if available.

    Stores the handle in module-level `_searchera_proc` so the reaper loop in
    `_keep_alive()` can monitor and restart it. See fixes.md #18.

    If port 21000 is already serving a healthy SearchEra (e.g. started
    externally, or still alive from a previous bridge instance), we skip
    the launch entirely and leave `_searchera_proc = None`. The reaper
    loop no-ops on that state — the externally-owned process is left
    alone, and we don't collide on the bind. Without this, the reaper
    would spin every 30s launching a process that immediately fails to
    bind, flooding searchera.log with bind errors.
    """
    global _searchera_proc

    # Auto-skip: probe /health on port 21000 before spawning. If it
    # answers 200, SearchEra is already up; return without launching
    # or registering _searchera_proc.
    try:
        async with httpx.AsyncClient(timeout=1.5) as _probe:
            _probe_resp = await _probe.get("http://localhost:21000/health")
        if _probe_resp.status_code == 200:
            logger.info(
                "SearchEra already serving on http://localhost:21000 (healthy) "
                "— not launching our own. Reaper will leave the external "
                "process alone. To force a fresh spawn, stop the existing "
                "process and restart this bridge."
            )
            return
        # Non-200: something else is bound. Log and continue — our
        # subprocess will fail to bind and the error will surface in
        # searchera.log.
        logger.warning(
            "SearchEra port 21000 answered HTTP %s on /health — will still "
            "attempt spawn (expect bind failure).", _probe_resp.status_code
        )
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
        pass  # port free or service unresponsive — fall through and try to launch
    except Exception as e:
        logger.warning("[SearchEra] pre-launch probe failed unexpectedly: %s", e)

    try:
        _searchera_proc = await _launch_searchera()
        logger.info("SearchEra API server launched (PID %d) on port 21000.", _searchera_proc.pid)
    except FileNotFoundError as e:
        logger.info("%s — skipping API launch.", e)
    except Exception as e:
        logger.error("Failed to start SearchEra API server: %s", e, exc_info=True)


async def _keep_alive():
    """Background supervisor — polls SearchEra PID and restarts on crash.

    Polls every 30 seconds. If `_searchera_proc` has exited (returncode is not
    None), attempts up to 3 restarts with exponential-ish backoff (5s, 10s, 15s).
    If all 3 fail, logs and continues — the next tick will retry. See fixes.md #18.
    """
    global _searchera_proc
    while True:
        await asyncio.sleep(30)

        if _searchera_proc is None:
            continue  # SearchEra was never started; nothing to monitor

        if _searchera_proc.returncode is None:
            continue  # still running

        # Process exited; attempt restart with exponential-ish backoff
        logger.warning(
            "SearchEra subprocess died (PID %d, exit code %s). Attempting restart.",
            _searchera_proc.pid, _searchera_proc.returncode,
        )

        restarted = False
        for attempt in range(3):
            backoff = 5 * (attempt + 1)
            logger.info("[Reaper] Retrying SearchEra startup (attempt %d/3, waiting %ds)...", attempt + 1, backoff)
            await asyncio.sleep(backoff)
            try:
                _searchera_proc = await _launch_searchera()
                logger.info("[Reaper] SearchEra restarted (PID %d).", _searchera_proc.pid)
                await asyncio.sleep(15)  # allow startup window before next health check
                restarted = True
                break
            except Exception as e:
                logger.error("[Reaper] SearchEra restart attempt %d failed: %s", attempt + 1, e)

        if not restarted:
            logger.error(
                "[Reaper] SearchEra could not be recovered after 3 attempts. "
                "Tool will remain inactive; reaper will retry on next tick."
            )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Sagent Bridge clean shutdown complete.")
