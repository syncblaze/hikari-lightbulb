# -*- coding: utf-8 -*-
# Copyright © tandemdude 2020-present
#
# This file is part of Lightbulb.
#
# Lightbulb is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Lightbulb is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Lightbulb. If not, see <https://www.gnu.org/licenses/>.
from __future__ import annotations

__all__ = ["serialise_command", "manage_application_commands"]

import logging
import typing as t

import hikari

if t.TYPE_CHECKING:
    from lightbulb import app as app_
    from lightbulb.commands import base


_LOGGER = logging.getLogger("lightbulb.internal")


class _GuildIDCollection:
    __slots__ = ("ids",)

    def __init__(self, ids: t.Optional[t.Union[t.Sequence[int], int]]):
        if ids is None:
            ids = []
        elif isinstance(ids, int):
            ids = [ids]
        self.ids: t.List[int] = list(ids)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, int):
            return NotImplemented
        return other in self.ids

    def __repr__(self) -> str:
        return f"Guilds({', '.join(str(i) for i in self.ids)})"


def _serialise_option(option: hikari.CommandOption) -> t.Dict[str, t.Any]:
    return {
        "type": option.type,
        "name": option.name,
        "description": option.description,
        "is_required": option.is_required,
        "choices": list(option.choices) if option.choices is not None else [],
        "options": [_serialise_option(o) for o in option.options] if option.options is not None else [],
        "channel_types": option.channel_types if option.channel_types is not None else [],
    }


def _serialise_hikari_command(command: hikari.Command) -> t.Dict[str, t.Any]:
    options = sorted(command.options or [], key=lambda o: o.name)
    return {
        "name": command.name,
        "description": command.description,
        "options": [_serialise_option(o) for o in options],
        "guild_id": command.guild_id,
    }


def _serialise_lightbulb_command(command: base.ApplicationCommand) -> t.Dict[str, t.Any]:
    options = sorted([o.as_application_command_option() for o in command.options.values()], key=lambda o: o.name)
    return {
        "name": command.name,
        "description": command.description,
        "options": [_serialise_option(o) for o in options],
        "guild_id": _GuildIDCollection(command.guilds),
    }


def serialise_command(command: t.Union[hikari.Command, base.ApplicationCommand]) -> t.Dict[str, t.Any]:
    if isinstance(command, hikari.Command):
        return _serialise_hikari_command(command)
    return _serialise_lightbulb_command(command)


def _compare_commands(cmd1: base.ApplicationCommand, cmd2: hikari.Command) -> bool:
    return serialise_command(cmd1) == serialise_command(cmd2)


async def manage_application_commands(app: app_.BotApp) -> None:
    assert app.application is not None

    all_commands = {c.name: c for c in app._slash_commands.values()}

    global_commands: t.Sequence[hikari.Command] = await app.rest.fetch_application_commands(app.application)
    for command in global_commands:
        registered_command = app.get_slash_command(command.name)
        if registered_command is None:
            if app._delete_unbound_commands:
                _LOGGER.info("Deleting global command %r as no implementation could be found", command.name)
                await command.delete()
            continue

        if not _compare_commands(registered_command, command):
            _LOGGER.info("Recreating global command %r as it appears to have changed", command.name)
            await registered_command._auto_create()
        else:
            _LOGGER.info("Not recreating global command %r as it does not appear to have changed", command.name)
        all_commands.pop(registered_command.name, None)

    all_guild_ids: t.Set[int] = set()
    for app_command in app._slash_commands.values():
        all_guild_ids.update(app_command.guilds)

    for guild_id in all_guild_ids:
        guild_commands: t.Sequence[hikari.Command] = await app.rest.fetch_application_commands(
            app.application, guild_id
        )
        for command in guild_commands:
            registered_command = app.get_slash_command(command.name)

            if registered_command is None:
                if app._delete_unbound_commands:
                    _LOGGER.info(
                        "Deleting command %r from guild %r as no implementation could be found",
                        command.name,
                        str(guild_id),
                    )
                    await command.delete()
                continue

            if not _compare_commands(registered_command, command):
                _LOGGER.info("Recreating guild command %r as it appears to have changed", command.name)
                await registered_command._auto_create()
            else:
                _LOGGER.info("Not recreating guild command %r as it does not appear to have changed", command.name)
            all_commands.pop(registered_command.name, None)

    for app_command in all_commands.values():
        _LOGGER.info("Creating command %r as it does not seem to exist yet", app_command.name)
        await app_command._auto_create()