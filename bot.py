#!/usr/bin/env python3
"""Discord <-> Claude Code bridge.

Listens on localhost HTTP for hook notifications, posts to a Discord
channel, and threads each session. Replies in threads are injected back
into the originating tmux pane (where Claude Code is running).
"""
import asyncio
import io
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path

import discord
from aiohttp import web
from discord.ext import commands

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("claude_discord_bridge")

HTTP_PORT = int(os.environ.get("BRIDGE_PORT", "7777"))
AUTO_ARCHIVE_MINUTES = 4320  # Discord valid values: 60, 1440, 4320, 10080
STATE_FILE = Path.home() / ".local/state/claude-discord-bridge/threads.json"
TRANSCRIPT_READ_LIMIT = 256 * 1024
INLINE_TRANSCRIPT_LIMIT = 1500
TMUX_BUFFER_PREFIX = "claude-bridge"
ALLOWED_MENTIONS = discord.AllowedMentions.none()
INBOX_ROOT = Path.home() / ".local/share/claude-discord-bridge/inbox"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
SAFE_ATTACHMENT_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def parse_allowed_user_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if item:
            try:
                ids.add(int(item))
            except ValueError as exc:
                raise RuntimeError(
                    f"DISCORD_ALLOWED_USER_IDS contains an invalid user ID: {item}"
                ) from exc
    if not ids:
        raise RuntimeError("DISCORD_ALLOWED_USER_IDS must contain at least one user ID")
    return ids


def parse_optional_user_id(name: str) -> int | None:
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return None

    value = raw.strip()
    try:
        user_id = int(value)
    except ValueError:
        logger.warning("%s contains an invalid user ID: %s", name, value)
        return None

    if user_id <= 0:
        logger.warning("%s contains an invalid user ID: %s", name, value)
        return None
    return user_id


def parse_optional_int(payload: dict, key: str) -> int | None:
    raw = payload.get(key)
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        logger.warning("Ignoring invalid integer payload field %s=%r", key, raw)
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
    else:
        logger.warning("Ignoring invalid integer payload field %s=%r", key, raw)
        return None
    if value <= 0:
        logger.warning("Ignoring invalid integer payload field %s=%r", key, raw)
        return None
    return value


TOKEN = required_env("DISCORD_BOT_TOKEN")
BRIDGE_TOKEN = required_env("BRIDGE_TOKEN")
CHANNEL_ID = int(required_env("DISCORD_CHANNEL_ID"))
DISCORD_ALLOWED_USER_IDS = parse_allowed_user_ids(required_env("DISCORD_ALLOWED_USER_IDS"))
DISCORD_PING_USER_ID = parse_optional_user_id("DISCORD_PING_USER_ID")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

threads: dict[int, dict] = {}
session_index: dict[str, int] = {}
session_locks: dict[str, asyncio.Lock] = {}
session_locks_guard = asyncio.Lock()
state_write_lock = asyncio.Lock()
transcript_cache: dict[str, tuple[float, int, str]] = {}


def load_state() -> None:
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to load state from %s", STATE_FILE)
        return
    threads.update({int(k): v for k, v in data.get("threads", {}).items()})
    session_index.update(data.get("sessions", {}))


def _save_state_sync(data: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, STATE_FILE)


async def save_state() -> None:
    async with state_write_lock:
        data = {
            "threads": {str(k): dict(v) for k, v in threads.items()},
            "sessions": dict(session_index),
        }
        await asyncio.to_thread(_save_state_sync, data)


def session_key(project: str, branch: str, agent: str, pane: str) -> str:
    return f"{project}|{branch}|{agent}|{pane}"


async def get_session_lock(key: str) -> asyncio.Lock:
    async with session_locks_guard:
        lock = session_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            session_locks[key] = lock
        return lock


def allowed_transcript_path(transcript_path: str) -> Path | None:
    if not transcript_path:
        return None

    raw_path = Path(transcript_path)
    if not raw_path.is_absolute():
        logger.warning("Rejected non-absolute transcript path: %s", transcript_path)
        return None

    path = raw_path.resolve(strict=False)
    allowed_roots = (
        (Path.home() / ".claude/projects").resolve(strict=False),
        (Path.home() / ".config/claude").resolve(strict=False),
    )
    if not any(path.is_relative_to(root) for root in allowed_roots):
        logger.warning("Rejected transcript path outside allowed roots: %s", transcript_path)
        return None
    return path


def _extract_last_assistant_text_sync(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return ""
    if not path.is_file():
        return ""

    key = str(path)
    cached = transcript_cache.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]

    last = ""
    try:
        with path.open("rb") as f:
            if stat.st_size > TRANSCRIPT_READ_LIMIT:
                f.seek(-TRANSCRIPT_READ_LIMIT, os.SEEK_END)
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""

    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            text = ""
        text = text.strip()
        if text:
            last = text

    transcript_cache[key] = (stat.st_mtime, stat.st_size, last)
    return last


async def extract_last_assistant_text(transcript_path: str) -> str:
    """Return the last assistant text content, or "" if unavailable."""
    path = allowed_transcript_path(transcript_path)
    if path is None:
        return ""
    return await asyncio.to_thread(_extract_last_assistant_text_sync, path)


def build_title(project: str, branch: str, agent: str) -> str:
    head = f"{project}/{branch}" if branch else project
    if agent and agent != "default":
        head += f" · {agent}"
    ts = datetime.now().strftime("%Y/%m/%d %-I:%M%p").lower()
    head += f" · {ts}"
    return head[:100]


async def find_or_create_thread(
    channel: discord.TextChannel,
    project: str,
    branch: str,
    agent: str,
    pane: str,
    pane_pid: int | None,
) -> discord.Thread:
    key = session_key(project, branch, agent, pane)
    lock = await get_session_lock(key)
    async with lock:
        tid = session_index.get(key)
        if tid:
            thread = channel.get_thread(tid)
            if thread is None:
                try:
                    thread = await bot.fetch_channel(tid)
                except discord.NotFound:
                    thread = None
            if thread and not thread.archived:
                info = threads.setdefault(thread.id, {})
                changed = False
                for field, value in (
                    ("tmux_pane", pane),
                    ("project", project),
                    ("branch", branch),
                    ("agent", agent),
                    ("tmux_pane_pid", pane_pid),
                ):
                    if value is not None and info.get(field) != value:
                        info[field] = value
                        changed = True
                if changed:
                    await save_state()
                return thread

        thread = await channel.create_thread(
            name=build_title(project, branch, agent),
            type=discord.ChannelType.public_thread,
            auto_archive_duration=AUTO_ARCHIVE_MINUTES,
        )
        threads[thread.id] = {
            "tmux_pane": pane,
            "project": project,
            "branch": branch,
            "agent": agent,
        }
        if pane_pid is not None:
            threads[thread.id]["tmux_pane_pid"] = pane_pid
        session_index[key] = thread.id
        await save_state()
        return thread


class TmuxCommandError(RuntimeError):
    pass


async def run_tmux_command(args: list[str], stdin_text: str | None = None) -> str:
    stdin = asyncio.subprocess.PIPE if stdin_text is not None else None
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin_text.encode() if stdin_text is not None else None),
            timeout=5,
        )
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise TmuxCommandError(f"{args[0]} timed out after 5s") from exc

    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        if not detail:
            detail = stdout.decode(errors="replace").strip()
        if not detail:
            detail = f"exit status {proc.returncode}"
        raise TmuxCommandError(detail)
    return stdout.decode(errors="replace")


def tmux_base_and_target(pane: str) -> tuple[list[str], str]:
    if "@" not in pane:
        return ["tmux"], pane
    pane_id, socket = pane.split("@", 1)
    return ["tmux", "-L", socket], pane_id


def pane_for_state(base_cmd: list[str], pane_id: str) -> str:
    if len(base_cmd) >= 3 and base_cmd[0] == "tmux" and base_cmd[1] == "-L":
        return f"{pane_id}@{base_cmd[2]}"
    return pane_id


async def resolve_pane(pane: str, pane_pid: int | None) -> tuple[list[str], str]:
    base_cmd, pane_id = tmux_base_and_target(pane)
    try:
        await run_tmux_command(
            base_cmd + ["display-message", "-p", "-t", pane_id, "#{pane_pid}"]
        )
        return base_cmd, pane_id
    except TmuxCommandError:
        pass

    if pane_pid is not None:
        for socket in ("default", "claude-bridge"):
            candidate_base = ["tmux"] if socket == "default" else ["tmux", "-L", socket]
            try:
                panes = await run_tmux_command(
                    candidate_base + ["list-panes", "-a", "-F", "#{pane_id} #{pane_pid}"]
                )
            except TmuxCommandError:
                continue
            for line in panes.splitlines():
                candidate_pane, _, candidate_pid = line.partition(" ")
                if candidate_pane and candidate_pid == str(pane_pid):
                    return candidate_base, candidate_pane

    raise TmuxCommandError(
        "pane lost; restart Claude in tmux and trigger a new notify"
    )


async def send_to_tmux(pane: str, pane_pid: int | None, text: str) -> str:
    base_cmd, pane_id = await resolve_pane(pane, pane_pid)
    buf = f"{TMUX_BUFFER_PREFIX}-{pane_id.lstrip('%').replace('@', '-')}"
    await run_tmux_command(base_cmd + ["load-buffer", "-b", buf, "-"], text)
    await run_tmux_command(
        base_cmd + ["paste-buffer", "-b", buf, "-d", "-t", pane_id]
    )
    await asyncio.sleep(0.3)
    await run_tmux_command(base_cmd + ["send-keys", "-t", pane_id, "Enter"])
    return pane_for_state(base_cmd, pane_id)


def escape_fence_collisions(text: str) -> str:
    return text.replace("```", "``\u200b`")


def sanitize_attachment_filename(filename: str) -> str:
    name = filename.replace("\\", "/").split("/")[-1].strip()
    return SAFE_ATTACHMENT_CHARS.sub("_", name)


async def save_image_attachments(message: discord.Message) -> list[Path]:
    saved: list[Path] = []
    if not message.attachments:
        return saved

    inbox = INBOX_ROOT / str(message.channel.id)
    try:
        inbox.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("Failed to create Discord attachment inbox %s", inbox)
        try:
            await message.add_reaction("❌")
        except discord.HTTPException:
            logger.exception("Failed to add attachment inbox failure reaction")
        return saved

    for attachment in message.attachments:
        content_type = attachment.content_type or ""
        if not content_type.startswith("image/"):
            logger.info(
                "Skipping non-image attachment %s content_type=%r",
                attachment.filename,
                attachment.content_type,
            )
            continue
        if attachment.size > MAX_ATTACHMENT_BYTES:
            logger.info(
                "Skipping oversized image attachment %s size=%s",
                attachment.filename,
                attachment.size,
            )
            continue

        name = sanitize_attachment_filename(attachment.filename)
        if not name:
            name = f"attachment_{attachment.id}"
        path = inbox / f"{int(time.time())}_{secrets.token_hex(4)}_{name}"
        try:
            await attachment.save(path)
        except Exception:
            logger.exception("Failed to save Discord attachment %s", attachment.filename)
            try:
                await message.add_reaction("❌")
            except discord.HTTPException:
                logger.exception("Failed to add attachment download failure reaction")
            continue
        saved.append(path.resolve())

    return saved


def build_tmux_payload(message_content: str, saved_images: list[Path]) -> str:
    image_refs = [f"@{path}" for path in saved_images]
    if not image_refs:
        return message_content
    if not message_content.strip():
        return " ".join(image_refs)
    return f"{message_content} {' '.join(image_refs)}"


@bot.event
async def on_ready() -> None:
    logger.info("Bot ready: %s", bot.user)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    if not isinstance(message.channel, discord.Thread):
        return
    if message.author.id not in DISCORD_ALLOWED_USER_IDS:
        await message.channel.send(
            "You are not authorized to reply to this Claude session.",
            allowed_mentions=ALLOWED_MENTIONS,
        )
        return
    info = threads.get(message.channel.id)
    if info is None:
        return
    pane = info.get("tmux_pane")
    if not pane:
        await message.channel.send(
            "No tmux pane recorded for this session.",
            allowed_mentions=ALLOWED_MENTIONS,
        )
        return
    pane_pid = parse_optional_int(info, "tmux_pane_pid")
    saved_images = await save_image_attachments(message)
    if not message.content.strip() and not saved_images:
        await message.add_reaction("❌")
        return
    tmux_payload = build_tmux_payload(message.content, saved_images)
    try:
        resolved_pane = await send_to_tmux(pane, pane_pid, tmux_payload)
        if resolved_pane != pane:
            info["tmux_pane"] = resolved_pane
            await save_state()
        await message.add_reaction("✅")
    except FileNotFoundError:
        await message.channel.send(
            "tmux not installed on bridge host.",
            allowed_mentions=ALLOWED_MENTIONS,
        )
    except TmuxCommandError as exc:
        await message.channel.send(
            f"tmux send failed: `{str(exc)[:1800]}`",
            allowed_mentions=ALLOWED_MENTIONS,
        )


async def handle_notify(request: web.Request) -> web.Response:
    if request.headers.get("X-Bridge-Token") != BRIDGE_TOKEN:
        logger.warning("/notify auth failure from %s", request.remote or "unknown")
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid json"}, status=400)

    project = payload.get("project", "unknown")
    branch = (payload.get("branch") or "").strip()
    agent = payload.get("agent", "default")
    event = payload.get("event", "notify")
    msg = (payload.get("message") or "").strip()
    pane = payload.get("tmux_pane") or ""
    pane_pid = parse_optional_int(payload, "tmux_pane_pid")
    transcript_path = payload.get("transcript_path") or ""

    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        return web.json_response({"error": "channel not found"}, status=500)

    thread = await find_or_create_thread(channel, project, branch, agent, pane, pane_pid)

    icon = {
        "stop": "✅",
        "notification": "❓",
        "subagent_stop": "\U0001f916",
        "error": "⚠️",
    }.get(event, "ℹ️")
    body = f"{icon} **{event}**"
    if msg:
        body += f"\n{msg[:500]}"

    file = None
    # Only attach last-assistant snippet for events where it carries new info.
    # Notification events repeat right after Stop, so transcript would duplicate.
    if event in ("stop", "subagent_stop"):
        last_assistant = await extract_last_assistant_text(transcript_path)
        if last_assistant and last_assistant != msg:
            snippet = escape_fence_collisions(last_assistant)
            fenced = f"\n```\n{snippet}\n```"
            if (
                len(snippet) > INLINE_TRANSCRIPT_LIMIT
                or len(body) + len(fenced) > 1990
            ):
                data = io.BytesIO(last_assistant.encode("utf-8"))
                file = discord.File(data, filename="transcript.txt")
                body += "\nTranscript attached."
            else:
                body += fenced

    allowed_mentions = ALLOWED_MENTIONS
    if event == "notification" and DISCORD_PING_USER_ID is not None:
        body = f"<@{DISCORD_PING_USER_ID}> {body}"
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            roles=False,
            replied_user=False,
            users=[discord.Object(id=DISCORD_PING_USER_ID)],
        )

    await thread.send(body[:1990], file=file, allowed_mentions=allowed_mentions)
    return web.json_response({"ok": True, "thread_id": thread.id})


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "ready": bot.is_ready()})


async def start_http() -> None:
    app = web.Application()
    app.router.add_post("/notify", handle_notify)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", HTTP_PORT)
    await site.start()
    logger.info("HTTP listening on 127.0.0.1:%s", HTTP_PORT)


async def main() -> None:
    load_state()
    await start_http()
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
