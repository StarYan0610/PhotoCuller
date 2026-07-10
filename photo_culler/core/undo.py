"""会话内撤销栈（命令模式，上限 100 条）"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DeleteCommand:
    """一次删除操作：记录 basename 与 manifest 记录，用于恢复。"""
    basename: str
    record: dict          # trash.new_delete_record 的返回值
    group_index: int      # 删除时在 Session.groups 中的索引


class UndoStack:
    def __init__(self, limit: int = 100) -> None:
        self._stack: list[DeleteCommand] = []
        self._limit = limit

    def push(self, cmd: DeleteCommand) -> None:
        self._stack.append(cmd)
        if len(self._stack) > self._limit:
            self._stack.pop(0)

    def pop(self) -> DeleteCommand | None:
        return self._stack.pop() if self._stack else None

    def __len__(self) -> int:
        return len(self._stack)
