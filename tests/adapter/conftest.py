"""
A DB-API server that is not there.

postgres, mysql and sql server need a server to answer, and a test that needs
one tests nothing until somebody has started it. What is worth pinning down
without one is everything between the tool call and the wire: which statement
goes out, with which parameters, and what the rows coming back are turned into.

So the driver is replaced, not the adapter — `_connect` hands back a connection
that answers from a script. Everything above it, including the statement the
audit trail records, is the real thing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

import pytest

# What the fake is told to say: (matcher, column names, rows). A matcher is a
# substring of the statement — whitespace collapsed, so a triple-quoted query
# can be matched by how it reads — or a predicate over statement and parameters.
Matcher = str | Callable[[str, tuple[Any, ...]], bool]
Rule = tuple[Matcher, Sequence[str], Sequence[Sequence[Any]]]


def squash(sql: str) -> str:
    """The statement on one line, so a matcher need not know its indentation."""
    return re.sub(r"\s+", " ", sql).strip()


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection
        self.description: list[tuple[str, ...]] | None = None
        self._rows: Sequence[Sequence[Any]] = []
        self.closed = False

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        statement = squash(sql)
        # Both what was passed and whether anything was: a driver that
        # interpolates client-side reads the statement's own `%` as a
        # placeholder the moment it is given parameters at all, empty or not.
        self._connection.passed.append(params)
        self._connection.executed.append((statement, tuple(params or ())))
        columns, rows = self._connection.answer(statement, tuple(params or ()))
        self.description = [(name,) for name in columns] if columns else None
        self._rows = rows

    def fetchall(self) -> list[Sequence[Any]]:
        return list(self._rows)

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    """
    Answers from a script, and records what it was asked.

    An unscripted statement answers with no rows rather than raising: most of
    what an adapter runs is bookkeeping the test at hand does not care about,
    and a rule per statement would bury the one being tested.
    """

    def __init__(self, rules: Sequence[Rule] = ()) -> None:
        self.rules = list(rules)
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.passed: list[Sequence[Any] | None] = []
        self.closed = False

    def answer(
        self, statement: str, params: tuple[Any, ...]
    ) -> tuple[Sequence[str], Sequence[Sequence[Any]]]:
        for matcher, columns, rows in self.rules:
            hit = (
                matcher(statement, params)
                if callable(matcher)
                else squash(matcher) in statement
            )
            if hit:
                return columns, rows
        return (), ()

    def cursor(self) -> FakeCursor:
        if self.closed:
            raise RuntimeError("the connection is closed")
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True

    # ---- what a test asks it afterwards ----

    @property
    def statements(self) -> list[str]:
        return [statement for statement, _ in self.executed]

    def find(self, needle: str) -> tuple[str, tuple[Any, ...]]:
        """The one statement holding `needle`, with its parameters."""
        hits = [pair for pair in self.executed if squash(needle) in pair[0]]
        assert hits, f"no statement holding {needle!r} in {self.statements}"
        return hits[-1]


@pytest.fixture
def connection() -> type[FakeConnection]:
    """The scripted connection itself, for a test that replaces the driver rather
    than the adapter's `_connect`."""
    return FakeConnection


@pytest.fixture
def wire() -> Callable[..., Any]:
    """
    An adapter of `cls` wired to a scripted connection instead of a server.

    Only `_connect` is replaced, so the adapter under test is otherwise the one
    that ships. Reach the connection back through `adapter._conn`.
    """

    def build(cls: type[Any], rules: Sequence[Rule] = (), **kwargs: Any) -> Any:
        connection = FakeConnection(rules)
        wired = type(
            f"Wired{cls.__name__}", (cls,), {"_connect": lambda self: connection}
        )
        return wired(**kwargs)

    return build
