from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Line:
    text: str
    score: float
    box: tuple[int, int, int, int]

    @property
    def height(self) -> int:
        return max(1, self.box[3] - self.box[1])

    @property
    def top(self) -> int:
        return self.box[1]

    def as_dict(self) -> dict:
        return {"text": self.text, "score": round(self.score, 4), "box": list(self.box)}


def reading_order(lines: list[Line]) -> list[Line]:
    rows: list[list[Line]] = []
    for line in sorted(lines, key=lambda item: (item.top, item.box[0])):
        if rows:
            anchor = rows[-1][0]
            overlap = min(anchor.box[3], line.box[3]) - max(anchor.box[1], line.box[1])
            if overlap > 0.6 * min(anchor.height, line.height):
                rows[-1].append(line)
                continue
        rows.append([line])
    return [line for row in rows for line in sorted(row, key=lambda item: item.box[0])]
