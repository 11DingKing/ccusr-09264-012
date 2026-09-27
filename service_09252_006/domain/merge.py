"""复核案件合并的领域规则（纯函数）。

合并请求的“同一性”由原案编号集合决定：与顺序、重复无关。
source_key 是该集合的规范化字符串，持久化层对其建唯一约束，
从而保证重复合并请求只会回放既有主案，绝不制造新主案。
"""
from __future__ import annotations


def normalize_source_ids(package_ids: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """去空白、去重、排序后的原案编号元组。"""
    return tuple(sorted({pid.strip() for pid in package_ids if pid and pid.strip()}))


def merge_source_key(package_ids: list[str] | tuple[str, ...]) -> str:
    """原案编号集合的规范化键；空集合返回空串（调用方负责拒绝）。"""
    return "|".join(normalize_source_ids(package_ids))
