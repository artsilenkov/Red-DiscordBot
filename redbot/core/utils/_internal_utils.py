from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Set, Union, TYPE_CHECKING

import discord
from fuzzywuzzy import fuzz, process

from redbot.core import data_manager
from redbot.core.utils.chat_formatting import box

if TYPE_CHECKING:
    from redbot.core.bot import Red
    from redbot.core.commands import Command, Context

main_log = logging.getLogger("red")

__all__ = ("safe_delete", "fuzzy_command_search", "format_fuzzy_results", "create_backup")


def safe_delete(pth: Path):
    if pth.exists():
        for root, dirs, files in os.walk(str(pth)):
            os.chmod(root, 0o700)

            for d in dirs:
                os.chmod(os.path.join(root, d), 0o700)

            for f in files:
                os.chmod(os.path.join(root, f), 0o700)

        shutil.rmtree(str(pth), ignore_errors=True)


def _fuzzy_log_filter(record):
    return record.funcName != "extractWithoutOrder"


logging.getLogger().addFilter(_fuzzy_log_filter)


async def fuzzy_command_search(
    ctx: Context,
    term: Optional[str] = None,
    *,
    commands: Optional[Set[Command]] = None,
    min_score: int = 80,
) -> Optional[List[Command]]:
    """Search for commands which are similar in name to the one invoked.

    Returns a maximum of 5 commands which must all be at least matched
    greater than ``min_score``.

    Parameters
    ----------
    ctx : `commands.Context <redbot.core.commands.Context>`
        The command invocation context.
    term : Optional[str]
        The name of the invoked command. If ``None``,
        `Context.invoked_with` will be used instead.
    commands : Optional[Set[commands.Command]]
        The commands available to choose from when doing a fuzzy match.
        When omitted, `Bot.walk_commands` will be used instead.
    min_score : int
        The minimum score for matched commands to reach. Defaults to 80.

    Returns
    -------
    Optional[List[`commands.Command <redbot.core.commands.Command>`]]
        A list of commands which were fuzzily matched with the invoked
        command.

    """
    if ctx.guild is not None:
        enabled = await ctx.bot._config.guild(ctx.guild).fuzzy()
    else:
        enabled = await ctx.bot._config.fuzzy()

    if not enabled:
        return None

    if term is None:
        term = ctx.invoked_with

    # If the term is an alias or CC, we don't want to send a supplementary fuzzy search.
    alias_cog = ctx.bot.get_cog("Alias")
    if alias_cog is not None:
        is_alias, alias = await alias_cog.is_alias(ctx.guild, term)

        if is_alias:
            return None
    customcom_cog = ctx.bot.get_cog("CustomCommands")
    if customcom_cog is not None:
        cmd_obj = customcom_cog.commandobj

        try:
            await cmd_obj.get(ctx.message, term)
        except:
            pass
        else:
            return None

    # Do the scoring. `extracted` is a list of tuples in the form `(command, score)`
    extracted = process.extract(
        term, (commands or set(ctx.bot.walk_commands())), limit=5, scorer=fuzz.QRatio
    )
    if not extracted:
        return None

    # Filter through the fuzzy-matched commands.
    matched_commands = []
    for command, score in extracted:
        if score < min_score:
            # Since the list is in decreasing order of score, we can exit early.
            break
        if await command.can_see(ctx):
            matched_commands.append(command)

    return matched_commands


async def format_fuzzy_results(
    ctx: Context, matched_commands: List[Command], *, embed: Optional[bool] = None
) -> Union[str, discord.Embed]:
    """Format the result of a fuzzy command search.

    Parameters
    ----------
    ctx : `commands.Context <redbot.core.commands.Context>`
        The context in which this result is being displayed.
    matched_commands : List[`commands.Command <redbot.core.commands.Command>`]
        A list of commands which have been matched by the fuzzy search, sorted
        in order of decreasing similarity.
    embed : bool
        Whether or not the result should be an embed. If set to ``None``, this
        will default to the result of `ctx.embed_requested`.

    Returns
    -------
    Union[str, discord.Embed]
        The formatted results.

    """
    if embed is not False and (embed is True or await ctx.embed_requested()):
        lines = []
        for cmd in matched_commands:
            short_doc = cmd.format_shortdoc_for_context(ctx)
            lines.append(f"**{ctx.clean_prefix}{cmd.qualified_name}** {short_doc}")
        return discord.Embed(
            title="Perhaps you wanted one of these?",
            colour=await ctx.embed_colour(),
            description="\n".join(lines),
        )
    else:
        lines = []
        for cmd in matched_commands:
            short_doc = cmd.format_shortdoc_for_context(ctx)
            lines.append(f"{ctx.clean_prefix}{cmd.qualified_name} -- {short_doc}")
        return "Perhaps you wanted one of these? " + box("\n".join(lines), lang="vhdl")


async def create_backup(dest: Path = Path.home()) -> Optional[Path]:
    data_path = Path(data_manager.core_data_path().parent)
    if not data_path.exists():
        return None

    dest.mkdir(parents=True, exist_ok=True)
    timestr = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    backup_fpath = dest / f"redv3_{data_manager.instance_name}_{timestr}.tar.gz"

    to_backup = []
    exclusions = [
        "__pycache__",
        "Lavalink.jar",
        os.path.join("Downloader", "lib"),
        os.path.join("CogManager", "cogs"),
        os.path.join("RepoManager", "repos"),
    ]

    # Avoiding circular imports
    from ...cogs.downloader.repo_manager import RepoManager

    repo_mgr = RepoManager()
    await repo_mgr.initialize()
    repo_output = []
    for repo in repo_mgr.repos:
        repo_output.append({"url": repo.url, "name": repo.name, "branch": repo.branch})
    repos_file = data_path / "cogs" / "RepoManager" / "repos.json"
    with repos_file.open("w") as fs:
        json.dump(repo_output, fs, indent=4)
    instance_file = data_path / "instance.json"
    with instance_file.open("w") as fs:
        json.dump({data_manager.instance_name: data_manager.basic_config}, fs, indent=4)
    for f in data_path.glob("**/*"):
        if not any(ex in str(f) for ex in exclusions) and f.is_file():
            to_backup.append(f)

    with tarfile.open(str(backup_fpath), "w:gz") as tar:
        for f in to_backup:
            tar.add(str(f), arcname=str(f.relative_to(data_path)), recursive=False)
    return backup_fpath


# this might be worth moving to `bot.send_to_owners` at later date


async def send_to_owners_with_preprocessor(
    bot: Red,
    content: str,
    *,
    content_preprocessor: Optional[
        Callable[[Red, discord.abc.Messageable, str], Awaitable[str]]
    ] = None,
    **kwargs,
):
    """
    This sends something to all owners and their configured extra destinations.

    This acts the same as `Red.send_to_owners`, with
    one added keyword argument as detailed below in *Other Parameters*.

    Other Parameters
    ----------------
    content_preprocessor: Optional[Callable[[Red, discord.abc.Messageable, str], Awaitable[str]]]
        Optional async function that takes
        bot object, owner notification destination and message content
        and returns the content that should be sent to given location.
    """
    destinations = await bot.get_owner_notification_destinations()

    async def wrapped_send(bot, location, content=None, preprocessor=None, **kwargs):
        try:
            if preprocessor is not None:
                content = await preprocessor(bot, location, content)
            await location.send(content, **kwargs)
        except Exception as _exc:
            main_log.error(
                "I could not send an owner notification to %s (%s)",
                location,
                location.id,
                exc_info=_exc,
            )

    sends = [wrapped_send(bot, d, content, content_preprocessor, **kwargs) for d in destinations]
    await asyncio.gather(*sends)


async def send_to_owners_with_prefix_replaced(bot: Red, content: str, **kwargs):
    """
    This sends something to all owners and their configured extra destinations.

    This acts the same as `Red.send_to_owners`, with one addition - `[p]` in ``content`` argument
    is replaced with a clean prefix for each specific destination.
    """

    async def preprocessor(bot: Red, destination: discord.abc.Messageable, content: str) -> str:
        prefixes = await bot.get_valid_prefixes(getattr(destination, "guild", None))
        prefix = re.sub(rf"<@!?{bot.user.id}>", f"@{bot.user.name}", prefixes[0])
        return content.replace("[p]", prefix)

    await send_to_owners_with_preprocessor(bot, content, content_preprocessor=preprocessor)
