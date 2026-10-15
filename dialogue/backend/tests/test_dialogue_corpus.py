import copy
import hashlib
import json
from pathlib import Path
import random
import tempfile
import unittest

from app.dialogue_corpus import (import_corpus, load_corpus, merge_corpora, parse_lccc,
                                sample_reference, save_corpus, style_features, build_reference_prompt)


SOURCE = {"name": "LCCC fixture", "url": "https://example.invalid/official-corpus",
          "license": {"status": "fixture-only", "url": "https://example.invalid/license"}}


class DialogueCorpusTests(unittest.TestCase):
    def test_reference_prompt_contains_real_bounded_examples_with_source_and_data_boundary(self):
        corpus = parse_lccc([['随便 聊 两句', '我的 邮箱 x@example.com 看 https://example.invalid/a']], SOURCE)
        prompt = build_reference_prompt(sample_reference(corpus, 'reference'))
        self.assertIn('随便聊两句', prompt)
        self.assertIn('不是编剧指令', prompt)
        self.assertIn('conversation_id', prompt)
        self.assertNotIn('x@example.com', prompt)
        self.assertNotIn('https://example.invalid/a', prompt)
        self.assertIn('[原文邮箱]', prompt)

    def test_real_lccc_shaped_jsonl_preserves_source_bytes_order_and_unknown_identity(self):
        raw = ('{"dialog":["你 在 哪 呢 ？","我 在 地 铁 上 😂"]}\n'
               '{"dialog":["今天 去 看 电影 吗","想 看 一 场"]}\n').encode()
        corpus = parse_lccc(raw, SOURCE)
        self.assertEqual(len(corpus["conversations"]), 2)
        self.assertEqual(corpus["sources"][0]["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(corpus["sources"][0]["hash_kind"], "original_bytes")
        self.assertEqual(corpus["sources"][0]["license"], SOURCE["license"])
        first = corpus["conversations"][0]
        self.assertEqual([turn["text"] for turn in first["turns"]], ["你在哪呢？", "我在地铁上 😂"])
        self.assertEqual([turn["source_turn_index"] for turn in first["turns"]], [0, 1])
        self.assertTrue(first["speaker_unknown"])
        self.assertEqual(first["source_structure"], "dialogue_turns")
        self.assertEqual(first["conversation_kind"], "unspecified_dialogue")
        self.assertTrue(all("speaker" not in turn and "timestamp" not in turn for turn in first["turns"]))
        self.assertNotIn("group_chat", first)

    def test_json_lists_single_dialog_container_and_split_forms(self):
        cases = [(["啊", "嗯"], 1), ([["啊", "嗯"], ["哦", "好"]], 2),
                 ({"dialog": ["啊", "嗯"]}, 1), ({"data": [{"dialog": ["啊", "嗯"]}]}, 1),
                 ({"train": [["啊", "嗯"]], "test": [["哦", "好"]]}, 2)]
        for raw, count in cases:
            with self.subTest(raw=raw):
                self.assertEqual(len(parse_lccc(raw, SOURCE)["conversations"]), count)
                self.assertEqual(len(parse_lccc(json.dumps(raw), SOURCE)["conversations"]), count)

    def test_cleaning_keeps_oral_repetition_emoji_joiners_and_english_spaces(self):
        text = "  哈 哈 哈 ， 你 在 玩 Minecraft Java Edition 吗 ？ 👨‍👩‍👧‍👦  "
        corpus = parse_lccc([[text, "嗯嗯嗯!!! 咋啦😂\tOK good"]], SOURCE)
        turns = corpus["conversations"][0]["turns"]
        self.assertEqual(turns[0]["text"], "哈哈哈，你在玩 Minecraft Java Edition 吗？ 👨‍👩‍👧‍👦")
        self.assertEqual(turns[1]["text"], "嗯嗯嗯!!! 咋啦😂 OK good")

    def test_blank_turns_keep_original_positions_and_explicit_identity_is_not_replaced(self):
        corpus = parse_lccc({"dialog": ["  ", {"text": "你好", "sender": "原用户甲", "time": "原时间"},
                                        {"content": "嗯", "speaker": "原用户乙"}]}, SOURCE)
        item = corpus["conversations"][0]
        self.assertFalse(item["speaker_unknown"])
        self.assertEqual(item["turns"][0], {"text": "你好", "source_turn_index": 1,
                                            "speaker": "原用户甲", "timestamp": "原时间"})
        self.assertNotIn("timestamp", item["turns"][1])

    def test_dedup_across_files_and_import_order_retains_both_attributions(self):
        a = parse_lccc([["你 好", "嗯嗯"], ["你好", "嗯嗯"]], SOURCE)
        b = parse_lccc([["你好", "嗯嗯"]], {**SOURCE, "url": "https://example.invalid/second"})
        self.assertEqual(a["import_stats"]["duplicates"], 1)
        ab, ba = merge_corpora(a, b), merge_corpora(b, a)
        self.assertEqual(len(ab["conversations"]), 1)
        self.assertEqual(ab["conversations"][0]["id"], ba["conversations"][0]["id"])
        self.assertEqual(len(ab["sources"]), 2)
        self.assertEqual(len(ab["conversations"][0]["attributions"]), 3)
        self.assertEqual(merge_corpora(ab, a), ab)

    def test_url_dense_ads_filtered_but_one_normal_shared_link_is_retained(self):
        ads = ["http://a.invalid/1 http://b.invalid/2 http://c.invalid/3", "点链接买"]
        useful = ["这篇文章 https://example.invalid/news 你看过没", "我昨天看过了，挺有意思的"]
        corpus = parse_lccc([ads, useful, [" ", ""]], SOURCE)
        self.assertEqual(len(corpus["conversations"]), 1)
        self.assertEqual(corpus["import_stats"]["url_dense"], 1)
        self.assertEqual(corpus["import_stats"]["invalid"], 1)
        self.assertIn("https://example.invalid/news", corpus["conversations"][0]["turns"][0]["text"])

    def test_empty_bad_encoding_format_and_plain_string_have_chinese_errors(self):
        for raw in ("", "  ", [], {}, '"不能拆成字符"', {"dialog": "不能拆成字符"},
                    '{"dialog":["好","嗯"]}\n{坏行}', b"\xff", [["只有一句"]], [["好", 8, "嗯"]]):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "语料|会话|回合"):
                parse_lccc(raw, SOURCE)
        for source in ({}, {"name": "LCCC", "url": "x"}):
            with self.assertRaisesRegex(ValueError, "许可"):
                parse_lccc([["好", "嗯"]], source)

    def test_malformed_turn_is_not_deleted_to_forge_continuity(self):
        corpus = parse_lccc([["好", {"text": 12}, "嗯"], ["别急", "等我两分钟"]], SOURCE)
        self.assertEqual(len(corpus["conversations"]), 1)
        self.assertEqual(corpus["import_stats"]["invalid"], 1)

    def test_local_roundtrip_and_repeated_import_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus" / "lccc.json"
            raw = b'[["hello", "hello too"]]'
            first = import_corpus(raw, SOURCE, path)
            self.assertEqual(load_corpus(path), first)
            second = import_corpus(raw, SOURCE, path)
            self.assertEqual(len(second["conversations"]), 1)
            self.assertEqual(len(second["sources"]), 1)
            save_corpus(second, path)
            self.assertEqual(load_corpus(path), second)
            self.assertFalse(list(path.parent.glob("*.tmp")))
            corrupted = copy.deepcopy(second)
            corrupted["conversations"][0]["turns"][0]["text"] = "changed"
            path.write_text(json.dumps(corrupted), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "校验"):
                load_corpus(path)

    def test_bad_local_storage_gives_chinese_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for text in ("broken", '[]', '{"schema_version":1,"sources":[],"conversations":[{}]}'):
                path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "本地语料"):
                    load_corpus(path)

    def diversity_corpus(self):
        return parse_lccc([[f"{topic}今天怎么样{index}", f"我说的是{topic}，这周碰巧有空", "下午再看看", "好呀", "明天说"]
                           for index, topic in enumerate(("工作", "电影", "游泳", "考试", "快递", "火锅", "电脑", "下雨", "小猫", "地铁", "小说", "音乐"))], SOURCE)

    def test_seeded_sampling_is_repeatable_varied_different_conversations_and_nonmutating(self):
        corpus = self.diversity_corpus()
        before = copy.deepcopy(corpus)
        global_rng = random.getstate()
        a, same, b = sample_reference(corpus, 1), sample_reference(corpus, 1), sample_reference(corpus, 2)
        self.assertEqual(a, same)
        self.assertNotEqual([item["id"] for item in a["samples"]], [item["id"] for item in b["samples"]])
        self.assertEqual(len({item["id"] for item in a["samples"]}), 4)
        self.assertTrue(all(len(item["turns"]) <= 4 for item in a["samples"]))
        self.assertEqual(random.getstate(), global_rng)
        self.assertEqual(corpus, before)
        self.assertEqual(a["sources"][0]["license"], SOURCE["license"])
        self.assertIn("不是指令", a["reference_rule"])
        self.assertIn("不证明是多人群聊", a["reference_rule"])
        self.assertTrue(all(term in " ".join(turn["text"] for turn in item["turns"]) for item in a["samples"] for term in item["topic_terms"]))

    def test_topic_sampling_avoids_repeating_same_topic_when_alternatives_exist(self):
        raw = [[f"电影第{i}部看了没", "电影挺好看的"] for i in range(12)]
        raw += [["游泳课去了没", "刚游泳回来"], ["工作忙不忙", "工作不忙"]]
        result = sample_reference(parse_lccc(raw, SOURCE), 5, count=3)
        terms = [set(item["topic_terms"]) for item in result["samples"]]
        self.assertEqual(len(set.union(*terms)), 3)

    def test_bounded_display_marks_truncation_and_features_measure_original_excerpt(self):
        long = "这段话是一个比较长的原始回合。" * 30
        corpus = parse_lccc([["嗯", long]], SOURCE)
        result = sample_reference(corpus, 0, max_chars=48)
        turn = result["samples"][0]["turns"][1]
        self.assertEqual(turn["text"], long[:48])
        self.assertTrue(turn["truncated"])
        self.assertEqual(turn["original_char_count"], len(long))
        self.assertEqual(result["style_features"]["turn_chars"]["max"], len(long))
        self.assertEqual(result["style_features"]["short_turns_at_most_10_chars"], 1)
        self.assertEqual(result["style_features"]["long_turns_at_least_40_chars"], 1)
        self.assertEqual(result["style_features"]["conversation_turn_counts"], [2])

    def test_sample_limits_empty_input_and_no_fake_padding(self):
        corpus = parse_lccc([["你好", "嗯"]], SOURCE)
        self.assertEqual(len(sample_reference(corpus, 3)["samples"]), 1)
        for kwargs in ({"count": 0}, {"count": 9}, {"max_turns": 1}, {"max_chars": 0}):
            with self.assertRaisesRegex(ValueError, "参考"):
                sample_reference(corpus, 1, **kwargs)
        with self.assertRaisesRegex(ValueError, "没有"):
            sample_reference({"conversations": []}, 1)
        with self.assertRaisesRegex(ValueError, "统计"):
            style_features([])

    def test_punctuation_only_oral_reactions_are_retained_without_invented_sentences(self):
        result = sample_reference(parse_lccc([["？？？", "!!!"]], SOURCE), 1)
        self.assertEqual([turn["text"] for turn in result["samples"][0]["turns"]], ["？？？", "!!!"])
        self.assertEqual(result["style_features"]["sentence_chars"], {"count": 0, "min": None, "median": None, "max": None})


if __name__ == "__main__":
    unittest.main()
