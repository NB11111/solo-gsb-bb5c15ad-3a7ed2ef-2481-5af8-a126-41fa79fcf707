"""配料占比区间顺序校验（临时预览）测试。

覆盖：
- 区间标注解析：单值 [12%]、区间 [10-20%]、全角括号 / 百分号、多种分隔符；
- 输入错误定位：缺少区间、格式错误、负值、下界大于上界，
  均携带完整嵌套路径，且多处错误一次全部返回；
- 区间传播（非组合枚举）：前向收紧上界、后向收紧下界，
  收紧后可行区间与 pinned（只能取单一边界值）标记；
- 不可行时返回最短冲突片段（多对冲突取最短）及所在组的完整嵌套路径；
- 预览只读：不产生任何审查记录。
"""
import os
import tempfile
import unittest

_DB_DIR = tempfile.mkdtemp(prefix="allergen-proportion-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "proportion.db")

from app import db  # noqa: E402
from app.proportion import ProportionError, check_proportions  # noqa: E402


def top_group(result):
    return result["groups"][0]


class IntervalParseTest(unittest.TestCase):
    def test_single_value_and_range(self):
        r = check_proportions("小麦粉[30-40%]，食盐[2%]")
        items = top_group(r)["items"]
        self.assertEqual(items[0]["interval"], [30, 40])
        self.assertEqual(items[1]["interval"], [2, 2])
        self.assertTrue(items[1]["pinned"])  # 单值即只能取该边界值

    def test_fullwidth_brackets_percent_and_dashes(self):
        r = check_proportions(
            "甲【30－40％】，乙[20~30%]，丙[10～15%]，丁[5—6%]，戊[1–2%]")
        intervals = [it["interval"] for it in top_group(r)["items"]]
        self.assertEqual(intervals,
                         [[30, 40], [20, 30], [10, 15], [5, 6], [1, 2]])

    def test_decimal_and_spaces(self):
        r = check_proportions("甲[12.5 - 30%]，乙[0.5%]")
        items = top_group(r)["items"]
        self.assertEqual(items[0]["interval"], [12.5, 30])
        self.assertEqual(items[1]["interval"], [0.5, 0.5])

    def test_prefix_stripped(self):
        r = check_proportions("配料表：小麦粉[30-40%]，食盐[1-2%]")
        self.assertEqual(top_group(r)["items"][0]["name"], "小麦粉")


class InputErrorTest(unittest.TestCase):
    def _errors(self, text):
        with self.assertRaises(ProportionError) as ctx:
            check_proportions(text)
        return ctx.exception.errors

    def test_missing_interval_located_by_path(self):
        errors = self._errors("小麦粉[30-40%]，白砂糖")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["path"], ["白砂糖"])
        self.assertIn("缺少占比区间", errors[0]["message"])

    def test_nested_missing_interval_full_path(self):
        errors = self._errors(
            "酱[30-50%]（奶粉[40-60%]（乳清粉），乳糖[40-60%]）")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["path"], ["酱", "奶粉", "乳清粉"])
        self.assertIn("缺少占比区间", errors[0]["message"])

    def test_format_errors(self):
        for bad in ("小麦粉[abc%]", "小麦粉[10]", "小麦粉[]", "小麦粉[10%]余量",
                    "小麦粉[1-2-3%]", "[30%]"):
            errors = self._errors(bad)
            self.assertEqual(len(errors), 1, bad)
            self.assertIn("格式错误" if bad != "[30%]" else "缺少原料名",
                          errors[0]["message"], bad)

    def test_negative_values(self):
        for bad in ("小麦粉[-5%]", "小麦粉[-5-10%]", "小麦粉[10--5%]"):
            errors = self._errors(bad)
            self.assertEqual(len(errors), 1, bad)
            self.assertIn("负值", errors[0]["message"], bad)

    def test_lower_greater_than_upper(self):
        errors = self._errors("小麦粉[30-20%]")
        self.assertEqual(len(errors), 1)
        self.assertIn("下界 30% 大于上界 20%", errors[0]["message"])

    def test_multiple_errors_collected_at_once(self):
        errors = self._errors(
            "小麦粉，酱[30-50%]（奶粉[40-60%]（乳清粉[-1%]），可可脂[5-2%]）")
        paths = [e["path"] for e in errors]
        self.assertIn(["小麦粉"], paths)
        self.assertIn(["酱", "奶粉", "乳清粉"], paths)
        self.assertIn(["酱", "可可脂"], paths)
        self.assertEqual(len(errors), 3)


class IntervalPropagationTest(unittest.TestCase):
    def test_feasible_group_tightens_intervals(self):
        # 前向收紧上界、后向收紧下界，结果经传递逐步收窄
        r = check_proportions("a[0-5%]，b[0-100%]，c[3-4%]")
        self.assertTrue(r["feasible"])
        items = top_group(r)["items"]
        self.assertEqual([it["tightened"] for it in items],
                         [[3, 5], [3, 5], [3, 4]])
        self.assertFalse(any(it["pinned"] for it in items))

    def test_pinned_to_single_boundary_value(self):
        # x1 ≥ x2 且 x1 ≤ 60、x2 ≥ 60 -> 两项都只能是 60%
        r = check_proportions("乳清粉[50-60%]，乳糖[60-75%]")
        self.assertTrue(r["feasible"])
        items = top_group(r)["items"]
        self.assertEqual(items[0]["tightened"], [60, 60])
        self.assertEqual(items[1]["tightened"], [60, 60])
        self.assertTrue(all(it["pinned"] for it in items))

    def test_lower_bound_propagates_upward(self):
        r = check_proportions("a[10-50%]，b[20-30%]")
        items = top_group(r)["items"]
        self.assertEqual(items[0]["tightened"], [20, 50])  # a 下界被 b 抬高
        self.assertEqual(items[1]["tightened"], [20, 30])

    def test_nested_groups_checked_independently(self):
        r = check_proportions(
            "酱[30-50%]（奶粉[40-60%]（乳清粉[50-70%]，乳糖[40-60%]），"
            "可可脂[20-30%]）")
        self.assertTrue(r["feasible"])
        self.assertEqual([g["path"] for g in r["groups"]],
                         [[], ["酱"], ["酱", "奶粉"]])
        nested = r["groups"][2]
        items = nested["items"]
        self.assertEqual(items[0]["tightened"], [50, 70])
        self.assertEqual(items[1]["tightened"], [40, 60])


class ConflictTest(unittest.TestCase):
    def test_infeasible_group_returns_shortest_segment(self):
        r = check_proportions("a[30-40%]，b[10-18%]，c[20-25%]，d[1-2%]")
        self.assertFalse(r["feasible"])
        group = top_group(r)
        self.assertFalse(group["feasible"])
        conflict = group["conflict"]
        self.assertEqual((conflict["start"], conflict["end"]), (1, 2))
        self.assertEqual([it["name"] for it in conflict["items"]], ["b", "c"])
        self.assertIn("18%", conflict["reason"])
        self.assertIn("20%", conflict["reason"])
        # 不可行组不给出收紧区间
        self.assertTrue(all(it["tightened"] is None for it in group["items"]))

    def test_shortest_segment_chosen_among_multiple_conflicts(self):
        # (b,c) 与 (c,d) 长度均为 1，(a,d) 更长；取最短的（同长取靠前者）
        r = check_proportions(
            "a[5-6%]，b[1-2%]，c[3-4%]，d[9-10%]，e[8-9%]")
        conflict = top_group(r)["conflict"]
        self.assertEqual((conflict["start"], conflict["end"]), (1, 2))
        self.assertEqual([it["name"] for it in conflict["items"]], ["b", "c"])

    def test_conflict_in_nested_group_carries_full_path(self):
        r = check_proportions(
            "小麦粉[30-40%]，酱[10-20%]（a[5-10%]，b[20-30%]），食盐[1-2%]")
        self.assertFalse(r["feasible"])
        nested = r["groups"][1]
        self.assertEqual(nested["path"], ["酱"])
        self.assertFalse(nested["feasible"])
        self.assertEqual([it["name"] for it in nested["conflict"]["items"]],
                         ["a", "b"])
        # 顶层组本身可行，互不影响
        self.assertTrue(r["groups"][0]["feasible"])

    def test_single_item_group_always_feasible(self):
        r = check_proportions("小麦粉[30-40%]")
        self.assertTrue(r["feasible"])
        item = top_group(r)["items"][0]
        self.assertEqual(item["tightened"], [30, 40])
        self.assertFalse(item["pinned"])


class PreviewIsReadonlyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.DB_PATH = os.path.join(_DB_DIR, "readonly.db")
        db.init_db()

    def test_check_creates_no_review_or_dict_entry(self):
        before = len(db.list_reviews())
        dict_before = {e["ingredient"] for e in db.list_dict()}
        check_proportions("小麦粉[30-40%]，神秘新原料[10-20%]，食盐[1-2%]")
        self.assertEqual(len(db.list_reviews()), before)
        self.assertEqual({e["ingredient"] for e in db.list_dict()}, dict_before)


if __name__ == "__main__":
    unittest.main()
