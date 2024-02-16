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

import collections.abc
import dataclasses
import logging
import typing as t

import hikari

from lightbulb.commands import execution
from lightbulb.commands import options as options_

if t.TYPE_CHECKING:
    from lightbulb import context as context_
    from lightbulb.commands import groups

__all__ = ["CommandData", "CommandMeta", "CommandBase", "UserCommand", "MessageCommand", "SlashCommand"]

T = t.TypeVar("T")
D = t.TypeVar("D")
CommandT = t.TypeVar("CommandT", bound="CommandBase")

LOGGER = logging.getLogger("lightbulb.commands")
_PRIMITIVE_OPTION_TYPES = (
    hikari.OptionType.STRING,
    hikari.OptionType.INTEGER,
    hikari.OptionType.FLOAT,
    hikari.OptionType.BOOLEAN,
    hikari.OptionType.MENTIONABLE,
)


@dataclasses.dataclass(slots=True)
class CommandData:
    """
    Dataclass for storing generic information about the command relevant
    for its creation and execution.
    """

    type: hikari.CommandType
    """The type of the command."""
    name: str
    """The name of the command."""
    description: str
    """The description of the command."""
    nsfw: bool
    """Whether the command is marked as nsfw."""
    localizations: t.Any  # TODO
    """Not yet implemented"""
    hooks: t.Set[execution.ExecutionHook]
    """Hooks to run prior to the invoke method being executed."""

    options: t.Mapping[str, options_.OptionData[t.Any]]
    """Map of option name to option data for the command options."""
    invoke_method: str
    """The attribute name of the invoke method for the command."""

    parent: t.Optional[t.Union[groups.Group, groups.SubGroup]] = dataclasses.field(init=False, default=None)
    """The group that the command belongs to, or :obj:`None` if not applicable."""

    def as_command_builder(self) -> hikari.api.CommandBuilder:
        """
        Convert the command data into a hikari command builder object.

        Returns:
            :obj:`hikari.api.CommandBuilder`: The builder object for this command data.
        """
        if self.type is hikari.CommandType.SLASH:
            bld = hikari.impl.SlashCommandBuilder(name=self.name, description=self.description)
            for option in self.options.values():
                bld.add_option(option.to_command_option())
            return bld

        return hikari.impl.ContextMenuCommandBuilder(type=self.type, name=self.name)

    def to_command_option(self) -> hikari.CommandOption:
        """
        Convert the command data into a sub-command command option.

        Returns:
            :obj:`hikari.CommandOption`: The sub-command option for this command data.
        """
        return hikari.CommandOption(
            type=hikari.OptionType.SUB_COMMAND,
            name=self.name,
            description=self.description,
            # TODO - localisations
            options=[option.to_command_option() for option in self.options.values()],
        )


class CommandMeta(type):
    """
    Metaclass for defining application commands.

    This metaclass handles the creation of your own application command implementation using
    the class parameters passed upon class declaration. It is not recommended that you
    use this metaclass directly - your commands should instead inherit from one of the built-in
    implementations (:obj:`~SlashCommand`, :obj:`~UserCommand`, :obj:`~MessageCommand`).

    Parameters:
        type (:obj:`hikari.CommandType`): The type of the command that the class implements. This should not
            be passed manually - it is filled automatically depending on the command implementation class that
            is subclassed. I.e. subclassing :obj:`SlashCommand` sets this parameter to :obj:`hikari.CommandType.SLASH`.
        name (:obj:`str`, required): The name of the command.
        description (:obj:`str`, optional): The description of the command. Only required for slash commands.
        nsfw (:obj:`bool`, optional): Whether the command should be marked as nsfw. Defaults to :obj:`False`.
        localizations (TODO, optional): Not yet implemented
        hooks (:obj:`~typing.Sequence` [ :obj:`~lightbulb.commands.execution.ExecutionHook` ], optional): The hooks to
            run before the command invocation function is executed. Defaults to an empty set.
    """

    __command_types: t.ClassVar[t.Dict[type, hikari.CommandType]] = {}

    @staticmethod
    def _is_option(item: t.Any) -> bool:
        return isinstance(item, options_.Option)

    def __new__(cls, cls_name: str, bases: t.Tuple[type, ...], attrs: t.Dict[str, t.Any], **kwargs: t.Any) -> type:
        cmd_type: hikari.CommandType
        # Bodge because I cannot figure out how to avoid initialising all the kwargs in our
        # own convenience classes any other way
        if "type" in kwargs:
            cmd_type = kwargs.pop("type")
            new_cls = super().__new__(cls, cls_name, bases, attrs, **kwargs)
            # Store the command type for our convenience class so that we can retrieve it when the
            # developer creates their own commands later
            CommandMeta.__command_types[new_cls] = cmd_type
            return new_cls

        # Find the convenience class that the new command inherits from so that we
        # can retrieve the command type that it implements
        base_cls = [base for base in bases if type(base) is CommandMeta]
        if len(base_cls) != 1:
            raise TypeError("commands must directly inherit from a single command class")
        cmd_type = CommandMeta.__command_types[base_cls[0]]

        cmd_name: str = kwargs.pop("name")
        description: str = kwargs.pop("description", "")
        # Descriptions are only required for slash commands
        if not description and cmd_type is hikari.CommandType.SLASH:
            raise TypeError("'description' is required for slash commands")

        nsfw: bool = kwargs.pop("nsfw", False)
        localizations: t.Any = kwargs.pop("localizations", None)

        raw_hooks: t.Any = kwargs.pop("hooks", None)
        if raw_hooks is not None and not isinstance(raw_hooks, collections.abc.Iterable):
            raise TypeError("'hooks' must be an iterable")

        hooks: t.Set[t.Any] = set(t.cast(t.Iterable[t.Any], raw_hooks) if raw_hooks is not None else [])
        if hooks and not any((isinstance(h, execution.ExecutionHook) for h in hooks)):
            raise TypeError("all hooks must be an instance of ExecutionHook")

        options: t.Dict[str, options_.OptionData[t.Any]] = {}
        invoke_method: t.Optional[str] = None
        # Iterate through new class attributes to find options and invoke method
        for name, item in attrs.items():
            if cls._is_option(item):
                options[name] = item._data
            elif hasattr(item, "__lb_cmd_invoke_method__"):
                invoke_method = name

        # Prevent command creation if no invoke method was found
        if invoke_method is None:
            raise TypeError("'invoke' registered method is required but could not be found")

        attrs["_command_data"] = CommandData(
            type=cmd_type,
            name=cmd_name,
            description=description,
            nsfw=nsfw,
            localizations=localizations,
            hooks=hooks,
            options=options,
            invoke_method=invoke_method,
        )

        return super().__new__(cls, cls_name, bases, attrs, **kwargs)


class CommandBase:
    """
    Base class that all commands should inherit from. Contains meta information about the
    command, execution information for each created instance, and various utility methods.
    """

    __slots__ = ("_current_context", "_resolved_option_cache")

    _command_data: t.ClassVar[CommandData]
    _current_context: t.Optional[context_.Context]
    _resolved_option_cache: t.MutableMapping[str, t.Any]

    def __new__(cls, *args: t.Any, **kwargs: t.Any) -> CommandBase:
        new = super().__new__(cls, *args, **kwargs)
        new._current_context = None
        new._resolved_option_cache = {}
        return new

    def _set_context(self, context: context_.Context) -> None:
        """
        Convenience method to set the current execution context and clear the resolved option cache.

        Args:
            context (:obj:`~lightbulb.context.Context`): The context being used for the current execution.

        Returns:
            :obj:`None`
        """
        self._current_context = context
        self._resolved_option_cache = {}

    def _resolve_option(self, option: options_.Option[T, D]) -> t.Union[T, D]:
        """
        Resolves the actual value for the given option from the command's current
        execution context. If the value has been resolved before and is available in the cache then
        the cached value is returned instead.

        Args:
            option (:obj:`~lightbulb.commands.options.Option`): The option to resolve the value for.

        Returns:
            :obj:`~typing.Union` [ ``T``, ``D`` ]: The resolved value for the given option.
        """
        context = self._current_context
        if context is None:
            raise RuntimeError("cannot resolve option if no context is available")

        if option._data.name in context.command._resolved_option_cache:
            return t.cast(T, context.command._resolved_option_cache[option._data.name])

        found = [opt for opt in context.options if opt.name == option._data.name]

        if not found or (option._data.type not in _PRIMITIVE_OPTION_TYPES and context.interaction.resolved is None):
            if option._data.default is hikari.UNDEFINED:
                # error lol
                raise ValueError("no option resolved and no default provided")

            return option._data.default

        if option._data.type in _PRIMITIVE_OPTION_TYPES:
            context.command._resolved_option_cache[option._data.name] = found[0].value
            return t.cast(T, found[0].value)

        snowflake = found[0].value
        resolved = context.interaction.resolved
        option_type = option._data.type

        assert isinstance(snowflake, hikari.Snowflake)
        assert resolved is not None

        resolved_option: t.Any
        if option_type is hikari.OptionType.USER:
            resolved_option = resolved.members.get(snowflake) or resolved.users[snowflake]
        elif option_type is hikari.OptionType.ROLE:
            resolved_option = resolved.roles[snowflake]
        elif option_type is hikari.OptionType.CHANNEL:
            resolved_option = resolved.channels[snowflake]
        elif option_type is hikari.OptionType.ATTACHMENT:
            resolved_option = resolved.attachments[snowflake]
        else:
            raise TypeError("unsupported option type passed")

        context.command._resolved_option_cache[option._data.name] = resolved_option
        return t.cast(T, resolved_option)

    @classmethod
    def as_command_builder(cls) -> hikari.api.CommandBuilder:
        """
        Convert the command into a hikari command builder object.

        Returns:
            :obj:`hikari.api.CommandBuilder`: The builder object for this command.
        """
        return cls._command_data.as_command_builder()

    @classmethod
    def to_command_option(cls) -> hikari.CommandOption:
        """
        Convert the command into a sub-command command option.

        Returns:
            :obj:`hikari.CommandOption`: The sub-command option for this command.
        """
        return cls._command_data.to_command_option()


class SlashCommand(CommandBase, metaclass=CommandMeta, type=hikari.CommandType.SLASH):
    """
    Base implementation of a slash command. This should be subclassed in order to create your own
    slash command.

    All subclasses **must** contain a method marked with the :obj:`lightbulb.commands.execution.invoke` decorator.

    Parameters:
        name (:obj:`str`, required): The name of the command.
        description (:obj:`str`, required): The description of the command.
        nsfw (:obj:`bool`, optional): Whether the command should be marked as nsfw. Defaults to :obj:`False`.
        localizations (TODO, optional): Not yet implemented
        hooks (:obj:`~typing.Sequence` [ :obj:`~lightbulb.commands.execution.ExecutionHook` ], optional): The hooks to
            run before the command invocation function is executed. Defaults to an empty set.

    Example:

        .. code-block:: python

            class Hello(
                lightbulb.SlashCommand,
                name="hello",
                description="makes the bot say hello",
                ...  # additional parameters
            ):
                @lightbulb.invoke
                async def invoke(self, ctx: lightbulb.Context):
                    await ctx.respond("Hello!")
    """

    __slots__ = ()


class UserCommand(CommandBase, metaclass=CommandMeta, type=hikari.CommandType.USER):
    """
    Base implementation of a slash command. This should be subclassed in order to create your own
    user command.

    All subclasses **must** contain a method marked with the :obj:`lightbulb.commands.execution.invoke` decorator.

    Parameters:
        name (:obj:`str`, required): The name of the command.
        nsfw (:obj:`bool`, optional): Whether the command should be marked as nsfw. Defaults to :obj:`False`.
        localizations (TODO, optional): Not yet implemented
        hooks (:obj:`~typing.Sequence` [ :obj:`~lightbulb.commands.execution.ExecutionHook` ], optional): The hooks to
            run before the command invocation function is executed. Defaults to an empty set.

    Example:

        .. code-block:: python

            class UserId(
                lightbulb.SlashCommand,
                name="userid",
                description="gets the ID of the user",
                ...  # additional parameters
            ):
                @lightbulb.invoke
                async def invoke(self, ctx: lightbulb.Context):
                    await ctx.respond(f"ID is {int(self.target.id)}")
    """

    __slots__ = ()

    target: hikari.User = t.cast(hikari.User, options_.ContextMenuOption(hikari.User))
    """The target user that the context menu command was executed on."""


class MessageCommand(CommandBase, metaclass=CommandMeta, type=hikari.CommandType.MESSAGE):
    """
    Base implementation of a slash command. This should be subclassed in order to create your own
    message command.

    All subclasses **must** contain a method marked with the :obj:`lightbulb.commands.execution.invoke` decorator.

    Parameters:
        name (:obj:`str`, required): The name of the command.
        nsfw (:obj:`bool`, optional): Whether the command should be marked as nsfw. Defaults to :obj:`False`.
        localizations (TODO, optional): Not yet implemented
        hooks (:obj:`~typing.Sequence` [ :obj:`~lightbulb.commands.execution.ExecutionHook` ], optional): The hooks to
            run before the command invocation function is executed. Defaults to an empty set.

    Example:

        .. code-block:: python

            class WordCount(
                lightbulb.MessageCommand,
                name="wordcount",
                description="counts the words in the message",
                ...  # additional parameters
            ):
                @lightbulb.invoke
                async def invoke(self, ctx: lightbulb.Context):
                    await ctx.respond(f"Message has {len(self.target.content.split()} words")
    """

    __slots__ = ()

    target: hikari.Message = t.cast(hikari.Message, options_.ContextMenuOption(hikari.Message))
    """The target message that the context menu command was executed on."""