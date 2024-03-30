# -*- coding: utf-8 -*-
# Copyright (c) 2023-present tandemdude
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
from __future__ import annotations

__all__ = ["Loadable", "Loader"]

import abc
import logging
import typing as t

import hikari
import svcs

from lightbulb.commands import commands
from lightbulb.commands import groups
from lightbulb.internal import di

if t.TYPE_CHECKING:
    from lightbulb import client as client_

CommandOrGroup: t.TypeAlias = t.Union[type[commands.CommandBase], groups.Group]
CommandOrGroupT = t.TypeVar("CommandOrGroupT", bound=CommandOrGroup)
EventT = t.TypeVar("EventT", bound=type[hikari.Event])

LOGGER = logging.getLogger("lightbulb.loaders")


class Loadable(abc.ABC):
    """Abstract class containing the logic required to add and remove a feature from a client instance."""

    __slots__ = ()

    @abc.abstractmethod
    async def load(self, client: client_.Client) -> None:
        """
        Add the feature to the client instance.

        Args:
            client (:obj:`~lightbulb.client.Client`): The client instance to add the feature to.

        Returns:
            :obj:`None`
        """

    async def unload(self, client: client_.Client) -> None:
        """
        Remove the feature from the client instance.

        Args:
            client (:obj:`~lightbulb.client.Client`): The client instance to remove the feature from.

        Returns:
            :obj:`None`
        """


class _CommandLoadable(Loadable):
    __slots__ = ("_command", "_guilds")

    def __init__(self, command: CommandOrGroup, guilds: t.Sequence[hikari.Snowflakeish] | None) -> None:
        self._command = command
        self._guilds = guilds

    async def load(self, client: client_.Client) -> None:
        client.register(self._command, guilds=self._guilds)


class _ListenerLoadable(Loadable):
    __slots__ = ("_callback", "_wrapped_callback", "_event_type")

    def __init__(self, callback: t.Callable[[EventT], t.Awaitable[None]], event_type: EventT) -> None:
        self._callback = callback
        self._event_type = event_type

        self._wrapped_callback: t.Callable[..., t.Awaitable[t.Any]] | None = None

    async def load(self, client: client_.Client) -> None:
        try:
            event_manager = await client.di.get_dependency(hikari.api.EventManager)
        except svcs.exceptions.ServiceNotFoundError as e:
            raise RuntimeError("cannot load listeners as client does not support event dispatching") from e

        async def _wrapped(*args: t.Any, **kwargs: t.Any) -> t.Any:
            with di.ensure_di_context(client.di):
                return await self._callback(*args, **kwargs)

        self._wrapped_callback = _wrapped if di.DI_ENABLED else None
        event_manager.subscribe(self._event_type, self._wrapped_callback or self._callback)  # type: ignore[reportArgumentType]

    async def unload(self, client: client_.Client) -> None:
        event_manager = await client.di.get_dependency(hikari.api.EventManager)
        event_manager.unsubscribe(self._event_type, self._wrapped_callback or self._callback)  # type: ignore[reportArgumentType]


class Loader:
    """Class used for loading features into the client from extensions."""

    __slots__ = ("_loadables",)

    def __init__(self) -> None:
        self._loadables: list[Loadable] = []

    async def add_to_client(self, client: client_.Client) -> None:
        """
        Add the features contained within this loader to the given client.

        Args:
            client (:obj:`~lightbulb.client.Client`): The client to add this loader's features to.

        Returns:
            :obj:`None`
        """
        for loadable in self._loadables:
            await loadable.load(client)

    async def remove_from_client(self, client: client_.Client) -> None:
        """
        Remove the features contained within this loader from the given client. If any single
        loadable's unload method raises an exception then the remaining loadables will still be unloaded.

        Args:
            client (:obj:`~lightbulb.client.Client`): The client to remove this loader's features from.

        Returns:
            :obj:`None`
        """
        for loadable in self._loadables:
            try:
                await loadable.unload(client)
            except Exception as e:
                LOGGER.warning("error while unloading loadable %r", loadable, exc_info=(type(e), e, e.__traceback__))

    @t.overload
    def command(
        self, *, guilds: t.Sequence[hikari.Snowflakeish] | None = None
    ) -> t.Callable[[CommandOrGroupT], CommandOrGroupT]: ...

    @t.overload
    def command(
        self, command: CommandOrGroupT, *, guilds: t.Sequence[hikari.Snowflakeish] | None = None
    ) -> CommandOrGroupT: ...

    def command(
        self, command: CommandOrGroupT | None = None, *, guilds: t.Sequence[hikari.Snowflakeish] | None = None
    ) -> CommandOrGroupT | t.Callable[[CommandOrGroupT], CommandOrGroupT]:
        """
        Register a command or group with this loader. Optionally, a sequence of guild ids can
        be provided to make the commands created in specific guilds only - overriding the value for
        default enabled guilds.

        This method can be used as a function, or a first or second order decorator.

        Args:
            command (:obj:`~typing.Union` [ :obj:`~typing.Type` [ :obj:`~lightbulb.commands.commands.CommandBase ], :obj:`~lightbulb.commands.groups.Group` ]): The
                command class or command group to register with the client.
            guilds (:obj:`~typing.Optional` [ :obj:`~typing.Sequence` [ :obj:`~hikari.Snowflakeish` ]]): The guilds
                to create the command or group in. If set to :obj:`None`, then this will fall back to the default
                enabled guilds. To override default enabled guilds and make the command or group global, this should
                be set to an empty sequence.

        Returns:
            The registered command or group, unchanged.

        Example:

            .. code-block:: python

                loader = lightbulb.Loader()

                # valid
                @loader.register
                # also valid
                @loader.register(guilds=[...])
                class Example(
                    lightbulb.SlashCommand,
                    ...
                ):
                    ...

                # also valid
                loader.register(Example, guilds=[...])
        """  # noqa: E501
        # Used as a function
        if command is not None:
            self._loadables.append(_CommandLoadable(command, guilds))
            return command

        # Used as a decorator
        def _inner(command_: CommandOrGroupT) -> CommandOrGroupT:
            return self.command(command_, guilds=guilds)

        return _inner

    def listener(
        self, event_type: EventT
    ) -> t.Callable[[t.Callable[..., t.Awaitable[None]]], t.Callable[[EventT], t.Awaitable[None]]]:
        """
        Decorator to register a listener with this loader. Also enables dependency injection on the listener
        callback.

        If an :obj:`hikari.api.event_manager.EventManager` instance is not available through dependency
        injection then adding this loader to the client will fail at runtime.

        Args:
            event_type (:obj:`~typing.Type` [ :obj:`hikari.Event` ]): The event class for the listener to listen to.

        Example:

            .. code-block:: python

                loader = lightbulb.Loader()

                @loader.listener(hikari.MessageCreateEvent)
                async def message_create_listener(event: hikari.MessageCreateEvent) -> None:
                    ...
        """

        def _inner(callback: t.Callable[..., t.Awaitable[None]]) -> t.Callable[[EventT], t.Awaitable[None]]:
            di_enabled = t.cast(t.Callable[[EventT], t.Awaitable[None]], di.with_di(callback))
            self._loadables.append(_ListenerLoadable(di_enabled, event_type))
            return di_enabled

        return _inner
