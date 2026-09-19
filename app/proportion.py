"""配料占比区间顺序校验（临时预览，不写入审查历史）。

复用现有嵌套配方解析（parser.parse_recipe），要求每个原料名后紧跟占比区间：

- 区间写法 ``[最小值-最大值%]``，单值可写 ``[12%]``；中英文方括号
  （``[]`` / ``【】``）、百分号（``%`` / ``％``）均可，分隔符支持
  ``- ~ – — ～ －``；
- 缺少区间、区间格式错误、负值、下界大于上界时逐条报错，并定位到
  完整嵌套路径（如 ``巧克力酱 › 奶粉 › 乳清粉``）。

对每组同级配料（顶层配料组，以及每个复合配料的子配料组）按标签书写
顺序建立占比**非递增**约束 x1 ≥ x2 ≥ … ≥ xn，并用**区间传播**（而非
组合枚举）判断整组是否存在可行赋值：

- 前向传播上界：U_i = min(u_i, U_{i-1})；若某项 l_i > U_i 则整组不可行；
- 后向传播下界：L_i = max(l_i, L_{i+1})；单调链上区间传播是完备的，
  [L_i, U_i] 即为该项在全部可行赋值中收紧后的精确可行区间；
- L_i == U_i 的项只能取单一边界值，结果中以 pinned 标出。

整组不可行时，冲突必然来自某对 m ≤ k 满足 u_m < l_k（顺序约束要求
x_m ≥ x_k，但上界小于下界）；取使 k − m 最小的 pair 对应的连续片段
[m..k] 作为**最短冲突片段**返回，并附上该组所在的完整嵌套路径。

结果仅用于页面预览：不读取过敏原字典，也不写入任何审查记录。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .parser import IngredientNode, parse_recipe

# 分隔符：连字符、波浪线、各类破折号、全角减号
DASHES = "-~–—～－"

_TRAILING_BRACKET_RE = re.compile(r"[\[【]([^\]】]*)[\]】]\s*$")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_NEGATIVE_HEAD_RE = re.compile(r"^[-－]")
_NEGATIVE_AFTER_DASH_RE = re.compile(r"[-~–—～－]\s*[-－]")


class ProportionError(ValueError):
    """占比区间输入错误（映射为 HTTP 400，detail 为逐条错误列表）。

    errors 中每条为 {"path": [...], "message": ...}，path 为从顶层配料
    到出错原料的完整嵌套路径。
    """

    def __init__(self, errors: List[dict]):
        self.errors = errors
        super().__init__("；".join(
            f"{' › '.join(e['path'])}：{e['message']}" for e in errors))


def _fmt_num(value: float):
    """占比数值输出：整数不写小数点。"""
    return int(value) if float(value).is_integer() else value


def _parse_interval_content(content: str) -> Tuple[float, float]:
    """解析方括号内的占比内容，返回 (下界, 上界)。

    负值、下界大于上界、其它格式错误分别抛出带中文说明的 ValueError。
    """
    text = content.strip()
    if not text.endswith(("%", "％")):
        raise ValueError(
            f"占比区间格式错误：「{content}」须以 % 结尾，"
            f"写法为 [最小值-最大值%] 或 [数值%]")
    core = text[:-1].strip()
    nums = _NUMBER_RE.findall(core)
    segs = _NUMBER_RE.split(core)
    valid = (
        (len(nums) == 1 and segs == ["", ""]) or
        (len(nums) == 2 and segs[0] == "" and segs[2] == ""
         and len(segs[1].strip()) == 1 and segs[1].strip() in DASHES))
    if valid:
        lo, hi = float(nums[0]), float(nums[-1])
        if lo > hi:
            raise ValueError(
                f"占比下界 {_fmt_num(lo)}% 大于上界 {_fmt_num(hi)}%")
        return lo, hi
    # 负值：区间以负号开头（[-5%] / [-5-10%]），或分隔符后再跟负号（[10--5%]）
    if _NEGATIVE_HEAD_RE.match(core) or _NEGATIVE_AFTER_DASH_RE.search(core):
        raise ValueError(f"占比不能为负值：「{content}」")
    raise ValueError(
        f"占比区间格式错误：「{content}」，写法为 [最小值-最大值%] 或 [数值%]")


def _extract_interval(raw_name: str, path: List[str],
                      errors: List[dict]):
    """从原料名尾部提取占比区间，返回 (净名称, (lo, hi) 或 None)。

    失败时向 errors 追加一条带完整嵌套路径的错误，并尽量返回净名称
    供子级路径使用。
    """
    name = raw_name.strip()
    match = _TRAILING_BRACKET_RE.search(name)
    if match is None:
        if any(c in name for c in "[]【】"):
            errors.append({"path": path + [name], "message":
                "占比区间格式错误：方括号须成对出现在原料名末尾，"
                "写法为 [最小值-最大值%] 或 [数值%]"})
        else:
            errors.append({"path": path + [name], "message":
                "缺少占比区间：请在原料名后标注 [最小值-最大值%] 或 [数值%]"})
        return name, None
    clean = name[:match.start()].strip()
    if not clean:
        errors.append({"path": path + [name],
                       "message": "占比区间前缺少原料名"})
        return name, None
    try:
        interval = _parse_interval_content(match.group(1))
    except ValueError as exc:
        errors.append({"path": path + [clean], "message": str(exc)})
        return clean, None
    return clean, interval


def _collect(nodes: List[IngredientNode], path: List[str],
             errors: List[dict], groups: List[tuple]) -> None:
    """先序遍历配料树：本级记为一个同级组，再递归各节点的子配料组。"""
    items = []
    for node in nodes:
        clean, interval = _extract_interval(node.name, path, errors)
        items.append((clean, interval, node))
    if items:
        groups.append((list(path),
                       [{"name": clean, "interval": interval}
                        for clean, interval, _ in items]))
    for clean, _, node in items:
        if node.children:
            _collect(node.children, path + [clean], errors, groups)


def _check_group(items: List[dict]) -> dict:
    """对一组同级配料（按标签顺序）做非递增顺序约束的区间传播校验。

    可行时给出每项收紧后的可行区间并标出 pinned（只能取单一边界值）；
    不可行时返回最短冲突片段（u_m < l_k 且 k − m 最小的连续片段 [m..k]）。
    """
    n = len(items)
    # 前向传播上界：U_i = min(u_i, U_{i-1})
    upper: List[float] = []
    for it in items:
        hi = it["interval"][1]
        upper.append(min(hi, upper[-1]) if upper else hi)
    feasible = all(it["interval"][0] <= upper[i]
                   for i, it in enumerate(items))
    base = [{
        "name": it["name"],
        "interval": [_fmt_num(it["interval"][0]), _fmt_num(it["interval"][1])],
        "tightened": None,
        "pinned": False,
    } for it in items]
    if feasible:
        # 后向传播下界：L_i = max(l_i, L_{i+1})
        lower = [0.0] * n
        lower[-1] = items[-1]["interval"][0]
        for i in range(n - 2, -1, -1):
            lower[i] = max(items[i]["interval"][0], lower[i + 1])
        for i, out in enumerate(base):
            lo, hi = lower[i], upper[i]
            out["tightened"] = [_fmt_num(lo), _fmt_num(hi)]
            out["pinned"] = lo == hi
        return {"feasible": True, "items": base, "conflict": None}
    # 不可行：对每个 k 取最大的 m（片段最短），再全局取 k − m 最小者
    best: Optional[Tuple[int, int]] = None
    for k in range(n):
        lo_k = items[k]["interval"][0]
        for m in range(k, -1, -1):
            if items[m]["interval"][1] < lo_k:
                if best is None or k - m < best[1] - best[0]:
                    best = (m, k)
                break
    m, k = best
    return {
        "feasible": False,
        "items": base,
        "conflict": {
            "start": m,
            "end": k,
            "items": base[m:k + 1],
            "reason": (
                f"「{items[m]['name']}」上界 "
                f"{_fmt_num(items[m]['interval'][1])}% 小于"
                f"「{items[k]['name']}」下界 "
                f"{_fmt_num(items[k]['interval'][0])}%，"
                f"按标签顺序的占比非递增约束无法满足"),
        },
    }


def check_proportions(text: str) -> dict:
    """配料占比区间顺序校验的完整计算（纯函数，不读写任何持久数据）。"""
    parsed = parse_recipe(text)
    errors: List[dict] = []
    collected: List[tuple] = []
    _collect(parsed, [], errors, collected)
    if errors:
        raise ProportionError(errors)
    groups = []
    for path, items in collected:
        group = _check_group(items)
        group["path"] = path
        groups.append(group)
    return {
        "feasible": all(g["feasible"] for g in groups),
        "group_count": len(groups),
        "groups": groups,
    }
