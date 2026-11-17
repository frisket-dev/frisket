"""Small specification model: no product imports, projections, SQL, or clocks."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Answer:
    owner: int
    state: str
    value: object = None


@dataclass
class Contribution:
    rows: frozenset[int]
    columns: tuple[int, ...]
    applied: bool = True
    writing: bool = True
    answers: dict[int, tuple[Answer, ...]] = field(default_factory=dict)


class Changes:
    def __init__(self):
        self.items: list[Contribution] = []

    def start(self, rows, columns) -> int:
        self.items.append(Contribution(frozenset(rows), tuple(columns)))
        return len(self.items)

    def publish(self, owner: int, row: int, values: tuple, state="success"):
        change = self.items[owner - 1]
        if not change.writing or not change.applied:
            raise ValueError("closed publication")
        if row not in change.rows or len(values) != len(change.columns):
            raise ValueError("outside output family")
        if row in change.answers:
            raise ValueError("immutable result")
        change.answers[row] = tuple(Answer(owner, state, value) for value in values)

    def close(self, owner: int):
        self.items[owner - 1].writing = False

    def deactivate(self, owner: int):
        change = self.items[owner - 1]
        change.applied = False
        change.writing = False

    def restore(self, owner: int):
        self.items[owner - 1].applied = True

    def read(self, row: int, column: int) -> Answer:
        for owner, change in reversed(list(enumerate(self.items, 1))):
            if (
                not change.applied
                or row not in change.rows
                or column not in change.columns
            ):
                continue
            if row in change.answers:
                return change.answers[row][change.columns.index(column)]
            return Answer(owner, "pending" if change.writing else "not_completed")
        return Answer(0, "missing")
