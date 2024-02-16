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

import collections
import dataclasses
import inspect
import typing as t

from lightbulb import exceptions
from lightbulb.internal import di

if t.TYPE_CHECKING:
    import typing_extensions as t_ex

    from lightbulb import context as context_

__all__ = ["ExecutionStep", "ExecutionSteps", "ExecutionHook", "ExecutionPipeline", "hook", "invoke"]

ExecutionHookFuncT: t_ex.TypeAlias = t.Callable[
    ["ExecutionPipeline", "context_.Context"], t.Union[t.Awaitable[None], None]
]


@dataclasses.dataclass(frozen=True, slots=True, eq=True)
class ExecutionStep:
    """
    Dataclass representing an execution step processed prior to the command invocation
    function being called.

    Args:
        name (:obj:`str`): The name of the execution step.
    """

    name: str
    """The name of the execution step"""

    __all_steps: t.ClassVar[t.Set[str]] = set()

    def __post_init__(self):
        if self.name in ExecutionStep.__all_steps:
            raise RuntimeError(f"a step with name {self.name} already exists")
        ExecutionStep.__all_steps.add(self.name)


@t.final
class ExecutionSteps:
    """Collection of the default execution steps lightbulb implements."""

    __slots__ = ()

    MAX_CONCURRENCY = ExecutionStep("MAX_CONCURRENCY")
    """Step for execution of maximum command concurrency logic."""
    CHECKS = ExecutionStep("CHECKS")
    """Step for execution of command check logic."""
    COOLDOWNS = ExecutionStep("COOLDOWNS")
    """Step for execution of command cooldown logic."""


@dataclasses.dataclass(frozen=True, slots=True, eq=True)
class ExecutionHook:
    """
    Dataclass representing a command execution hook executed before the invocation method is called.

    Args:
        step (:obj:`~ExecutionStep`): The step that this hook should be run during.
        func: The function that this hook executes. May either be synchronous or asynchronous, and **must** take
            (at least) two arguments - and instance of :obj:`~ExecutionPipeline` and :obj:`~lightbulb.context.Context`
            respectively.
    """

    step: ExecutionStep
    """The step that this hook should be run during."""
    func: ExecutionHookFuncT
    """The function that this hook executes."""

    async def __call__(self, pipeline: ExecutionPipeline, context: context_.Context) -> None:
        maybe_await = self.func(pipeline, context)
        if inspect.isawaitable(maybe_await):
            await maybe_await


class ExecutionPipeline:
    """
    Class representing an entire command execution flow. Handles processing command hooks, including
    failure handling and collecting, as well as the calling of the command invocation function if
    all hooks succeed.
    """

    __slots__ = ("_context", "_remaining", "_hooks", "_current_step", "_current_hook", "_failures", "_abort")

    def __init__(self, context: context_.Context, order: t.Sequence[ExecutionStep]) -> None:
        self._context = context
        self._remaining = list(order)

        self._hooks: t.Dict[ExecutionStep, t.List[ExecutionHook]] = collections.defaultdict(list)
        for hook in context.command_data.hooks:
            self._hooks[hook.step].append(hook)

        self._current_step: t.Optional[ExecutionStep] = None
        self._current_hook: t.Optional[ExecutionHook] = None

        self._failures: t.Dict[ExecutionStep, t.List[exceptions.HookFailedException]] = collections.defaultdict(list)
        self._abort: t.Optional[exceptions.HookFailedException] = None

    @property
    def failed(self) -> bool:
        """
        Whether this pipeline has failed.

        A pipeline is considered failed if any single hook execution failed, or
        a hook caused the pipeline to be aborted.
        """
        return bool(self._failures) or self.aborted

    @property
    def aborted(self) -> bool:
        """Whether this pipeline was aborted."""
        return self._abort is not None

    def _next_step(self) -> t.Optional[ExecutionStep]:
        """
        Return the next execution step to run, or :obj:`None` if the remaining execution steps
        have been exhausted.

        Returns:
            :obj:`~typing.Optional` [ :obj:`~ExecutionStep` ]: The new execution step, or :obj:`None` if there
                are none remaining
        """
        if self._remaining:
            return self._remaining.pop(0)
        return None

    async def _run(self) -> None:
        """
        Run the pipeline. Does not reset the state if called multiple times.
        To run the command again a new pipeline should be created.

        Returns:
            :obj:`None`

        Raises:
            :obj:`~lightbulb.exceptions.PipelineFailedException`: If the pipeline failed due to hook failures, or
                the command invocation function raised an exception.
        """
        self._current_step = self._next_step()
        while self._current_step is not None and not self.aborted:
            step_hooks = list(self._hooks.get(self._current_step, []))
            while step_hooks and not self.aborted:
                self._current_hook = step_hooks.pop(0)
                try:
                    await self._current_hook(self, self._context)
                except Exception as e:
                    self.fail(e)

            if self.failed:
                break

            self._current_step = self._next_step()

        if self.failed:
            causes = [failure for step_failures in self._failures.values() for failure in step_failures]
            if self._abort is not None:
                causes.append(self._abort)

            assert self._current_step is not None
            raise exceptions.PipelineFailedException(causes, self)

        try:
            await getattr(self._context.command, self._context.command_data.invoke_method)(self._context)
        except Exception as e:
            raise exceptions.PipelineFailedException([exceptions.InvocationFailedException(e, self._context)], self)

    def fail(self, exc: t.Union[str, Exception], abort: bool = False) -> None:
        """
        Notify the pipeline of a failure in an execution hook. Optionally, the failure can cause
        the pipeline to be aborted causing no further hooks in the current step to be run.

        Args:
            exc (:obj:`~typing.Union` [ :obj:`str`, :obj:`Exception` ]): Message or exception to include
                with the failure.
            abort (:obj:`bool`): Whether this failure should abort the pipeline. If :obj:`True`, no further hooks
                will be run in the current step and an exception will be propagated.

        Returns:
            :obj:`None`

        Note:
            If ``abort`` is :obj:`False` (the default) then remaining hooks in the current step will still be run.
            All failures will be collected and propagated once the current step has been completed.
        """
        if not isinstance(exc, Exception):
            exc = RuntimeError(exc)

        assert self._current_step is not None
        assert self._current_hook is not None

        hook_exc = exceptions.HookFailedException(exc, self._current_hook, abort)

        if abort:
            self._abort = hook_exc
            return

        self._failures[self._current_step].append(hook_exc)


def hook(step: ExecutionStep) -> t.Callable[[ExecutionHookFuncT], ExecutionHook]:
    """
    Second order decorator to convert a function into an execution hook for the given
    step. Also enables dependency injection on the decorated function.

    The decorated function can be either synchronous or asyncronous and **must** take at minimum the
    two arguments seen below. ``pl`` is an instance of :obj:`~ExecutionPipeline` which is used to manage
    the command execution flow, and ``ctx`` is an instance of :obj:`~lightbulb.context.Context` which contains
    information about the current invocation.

    .. code-block:: python

        def example_hook(pl: lightbulb.ExecutionPipeline, ctx: lightbulb.Context) -> None:
            # Hook logic
            ...

    Args:
        step (:obj:`~ExecutionStep`): The step that this hook should be run during.

    Returns:
        :obj:`~ExecutionHook`: The created execution hook.

    Example:
        To implement a custom hook to block execution of a command on days other than monday.

        .. code-block:: python

            @lightbulb.hook(lightbulb.ExecutionStep.CHECKS)
            def only_on_mondays(pl: lightbulb.ExecutionPipeline, _: lightbulb.Context) -> None:
                # Check if today is Monday (0)
                if datetime.date.today().weekday() != 0:
                    # Fail the pipeline execution
                    pl.fail("This command can only be used on mondays")
    """

    def inner(func: ExecutionHookFuncT) -> ExecutionHook:
        if not isinstance(func, di.LazyInjecting):
            func = di.LazyInjecting(func)  # type: ignore[reportArgumentType]

        return ExecutionHook(step, func)

    return inner


def invoke(func: t.Callable[..., t.Awaitable[t.Any]]) -> t.Callable[[context_.Context], t.Awaitable[t.Any]]:
    """
    First order decorator to mark a method as the invocation method to be used for the command. Also enables
    dependency injection on the decorated method. The decorated method **must** have the first parameter (non-self)
    accepting an instance of :obj:`~lightbulb.context.Context`. Remaining parameters will attempt to be
    dependency injected.

    Args:
        func: The method to be marked as the command invocation method.

    Returns:
        The decorated method with dependency injection enabled.

    Example:

        .. code-block:: python

            class ExampleCommand(
                lightbulb.SlashCommand,
                name="example",
                description="example"
            ):
                @lightbulb.invoke
                async def invoke(self, ctx: lightbulb.Context) -> None:
                    await ctx.respond("example")
    """
    if not isinstance(func, di.LazyInjecting):
        func = di.LazyInjecting(func)

    setattr(func, "__lb_cmd_invoke_method__", "_")
    return func