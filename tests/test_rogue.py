from __future__ import annotations

import unittest

from ArknightsUID.utils.rogue import flatten_rogue, parse_rogue


class RogueParseTest(unittest.TestCase):
    def test_parse_rogue_returns_none_for_non_dict(self) -> None:
        self.assertIsNone(parse_rogue(None))
        self.assertIsNone(parse_rogue("x"))
        self.assertIsNone(parse_rogue([]))

    def test_parse_rogue_returns_none_for_empty(self) -> None:
        self.assertIsNone(parse_rogue({}))

    def test_parse_rogue_exact_records_from_real_dump(self) -> None:
        data = {
            "records": [
                {"rogueId": "rogue_1", "relicCnt": 229,
                 "bank": {"current": 445, "record": 540},
                 "clearTime": 69, "bpLevel": 155,
                 "medal": {"total": 11, "current": 11}},
                {"rogueId": "rogue_3", "relicCnt": 263,
                 "bank": {"current": 867, "record": 999},
                 "clearTime": 26, "bpLevel": 155,
                 "medal": {"total": 12, "current": 12}},
            ]
        }
        info = parse_rogue(data)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.raw, data)  # raw 兜底保留
        self.assertEqual(len(info.records), 2)

        r = info.records[0]
        self.assertEqual(r.rogue_id, "rogue_1")
        self.assertEqual(r.relic_cnt, 229)
        self.assertEqual(r.clear_time, 69)
        self.assertEqual(r.bp_level, 155)
        self.assertEqual(r.bank.current, 445)
        self.assertEqual(r.bank.record, 540)
        self.assertEqual(r.medal.total, 11)
        self.assertEqual(r.medal.current, 11)

        self.assertEqual(info.records[1].rogue_id, "rogue_3")
        self.assertEqual(info.records[1].bank.current, 867)

    def test_parse_rogue_tolerates_missing_fields(self) -> None:
        info = parse_rogue({"records": [{"rogueId": "rogue_1"}]})
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(len(info.records), 1)
        self.assertEqual(info.records[0].rogue_id, "rogue_1")
        self.assertEqual(info.records[0].relic_cnt, 0)  # 缺失归零
        self.assertEqual(info.records[0].bank.current, 0)

    def test_parse_rogue_no_records_still_returns_info(self) -> None:
        # 有 rogue dict 但无 records：返回空 records 的 RogueInfo（命令走 flatten 兜底）
        info = parse_rogue({"foo": "bar"})
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.records, [])


class RogueFlattenTest(unittest.TestCase):
    def test_flatten_scalars_and_skip_empty(self) -> None:
        flat = flatten_rogue({"a": 1, "b": "x", "c": ""})
        labels = dict(flat)
        self.assertEqual(labels["a"], "1")
        self.assertEqual(labels["b"], "x")
        self.assertNotIn("c", labels)

    def test_flatten_nested_dict(self) -> None:
        flat = flatten_rogue({"outer": {"inner": 42}})
        self.assertIn(("outer.inner", "42"), flat)

    def test_flatten_list(self) -> None:
        flat = flatten_rogue({"items": ["x", "y"]})
        self.assertIn(("items[0]", "x"), flat)
        self.assertIn(("items[1]", "y"), flat)

    def test_flatten_respects_max_depth(self) -> None:
        deep = {"a": {"b": {"c": {"d": "value"}}}}
        flat = flatten_rogue(deep, max_depth=2)
        labels = [label for label, _ in flat]
        self.assertNotIn("a.b.c.d", labels)

    def test_flatten_empty_structures_yield_nothing(self) -> None:
        self.assertEqual(flatten_rogue({}), [])
        self.assertEqual(flatten_rogue({"a": {}}), [])


if __name__ == "__main__":
    unittest.main()
