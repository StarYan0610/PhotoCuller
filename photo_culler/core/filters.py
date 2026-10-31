"""筛选条件：日期（天粒度）与相机星级评分，均支持多选。纯逻辑，便于单测。"""
from __future__ import annotations

from collections.abc import Collection


def passes(group, days: Collection[str] | None = None,
           ratings: Collection[int] | None = None) -> bool:
    """判断组是否通过筛选（多选之间是"或"关系，两组条件之间是"与"）。

    days: 空/None = 全部日期；否则为 'YYYY-MM-DD' 集合（按拍摄日，缺则文件时间）。
    ratings: 空/None = 全部评分；否则为星值集合，0 表示未评分、1-5 表示对应星级。
    """
    if days and group.day not in days:
        return False
    if ratings and group.rating not in ratings:
        return False
    return True


def visible_indexes(groups, days: Collection[str] | None = None,
                    ratings: Collection[int] | None = None) -> list[int]:
    """返回通过筛选的组在 groups 中的下标列表（保持原顺序）。"""
    return [i for i, g in enumerate(groups) if passes(g, days, ratings)]
