import unittest

from app.core import export_xlsx, read_source
from app.media_contract import decode_media, screenshot_errors


def source():
    return {"messages": [{"seq": 1, "sender": "坐席", "content": "打开 https://example.invalid/task，完成后请截图发到群里"}]}


def message(seq, sender="客户1", text="知道了", kind="文本", code="-"):
    return {"seq": seq, "sender": sender, **decode_media(kind, code, text)}


def run(messages, status="running", version=4):
    return {"config": {"dialogue_flow_version": version}, "messages": messages, "status": status}


def announcement():
    return message(1, "坐席", "打开 https://example.invalid/task，完成后请截图发到群里")


class MediaContractTests(unittest.TestCase):
    def test_decode_text_and_image_metadata_are_separate(self):
        for code in ("-", "0", "", None):
            self.assertEqual(decode_media("文本", code, " 普通内容 "),
                             {"type": "文本", "content": "普通内容", "image_code": None, "image_mode": None})
        self.assertEqual(decode_media("图片", "SCREENSHOT_customer-1_a", "") ,
                         {"type": "图片", "content": "", "image_code": "SCREENSHOT_customer-1_a", "image_mode": "素材占位"})
        self.assertEqual(decode_media("图片+文本", "SCREENSHOT_one", "截图已发到群里了")["content"], "截图已发到群里了")

    def test_invalid_type_code_or_missing_content_rejected(self):
        cases = [("视频", "-", "内容"), ("文本", "SCREENSHOT_one", "内容"), ("文本", "-", " "),
                 ("图片+文本", "SCREENSHOT_one", ""), ("图片", "SCREENSHOT_", ""),
                 ("图片", "SCREENSHOT_中文", ""), ("图片", "SCREENSHOT_a.png", ""),
                 ("图片", "https://example.invalid/a.png", ""), ("图片", "-", "")]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                decode_media(*case)

    def test_same_message_and_earlier_image_prove_own_claim(self):
        for messages in ([announcement(), message(2, text="我的截图已经发到群里了", kind="图片+文本", code="SCREENSHOT_one")],
                         [announcement(), message(2, text="", kind="图片", code="SCREENSHOT_one"),
                          message(3, text="我已经把截图发到群里了")]):
            self.assertEqual(screenshot_errors(source(), run(messages, "completed")), [])

    def test_other_author_and_future_images_do_not_prove_claim(self):
        for messages in ([announcement(), message(2, "客户2", "", "图片", "SCREENSHOT_two"),
                          message(3, text="我的截图已发群里了")],
                         [announcement(), message(2, text="我的截图已发群里了"),
                          message(3, text="", kind="图片", code="SCREENSHOT_one")]):
            errors = screenshot_errors(source(), run(messages, "completed"))
            self.assertTrue(any("没有本人先前或同条截图事件" in error for error in errors), errors)

    def test_negative_planned_and_question_text_is_not_completion(self):
        for text in ("我还没发截图到群里", "我的截图还没发群里呢", "我没有分享截图", "我准备把截图发到群里", "我要发截图到群里",
                     "截图发群吗", "我的截图已发群里了吗？", "截图怎么发到群里", "我打算分享截图", "我不发截图到群里", "他的截图已发群里了",
                     "你的截图已发群里了", "如果截图发群了就不用重复发"):
            with self.subTest(text=text):
                self.assertEqual(screenshot_errors(source(), run([announcement(), message(2, text=text)])), [])

    def test_explicit_share_without_group_word_still_needs_own_event(self):
        for text in ("我的截图已经分享了", "我把截图分享了", "我的截图分享过了"):
            with self.subTest(text=text):
                errors = screenshot_errors(source(), run([announcement(), message(2, text=text)]))
                self.assertTrue(any("没有本人先前或同条截图事件" in error for error in errors), errors)

    def test_image_requires_both_prior_entry_and_screenshot_instruction(self):
        image = message(2, text="", kind="图片", code="SCREENSHOT_one")
        for first, reason in (("请截图发到群里", "入口前"), ("打开 https://example.invalid/task", "截图要求前"),
                              ("大家好", "入口前")):
            with self.subTest(first=first):
                errors = screenshot_errors(source(), run([message(1, "坐席", first), image, {**announcement(), "seq": 3}]))
                self.assertTrue(any(reason in error for error in errors), errors)

    def test_same_seat_message_cannot_authorize_its_image(self):
        item = message(1, "坐席", announcement()["content"], "图片+文本", "SCREENSHOT_seat")
        errors = screenshot_errors(source(), run([item]))
        self.assertTrue(any("入口前" in error for error in errors), errors)
        self.assertTrue(any("截图要求前" in error for error in errors), errors)

    def test_wrong_placeholder_mode_and_text_image_metadata_rejected(self):
        for image in ({**message(2, text="", kind="图片", code="SCREENSHOT_one"), "image_mode": "真实截图"},
                      {**message(2), "image_mode": "素材占位"},
                      {**message(2), "image_code": "SCREENSHOT_one"}):
            self.assertTrue(screenshot_errors(source(), run([announcement(), image])))

    def test_completed_requires_participant_image_running_does_not(self):
        messages = [announcement()]
        self.assertEqual(screenshot_errors(source(), run(messages)), [])
        self.assertTrue(any("至少需要一位参与者" in error for error in screenshot_errors(source(), run(messages, "completed"))))
        messages.append(message(2, "坐席", "", "图片", "SCREENSHOT_seat"))
        self.assertTrue(any("至少需要一位参与者" in error for error in screenshot_errors(source(), run(messages, "completed"))))

    def test_without_source_url_no_url_is_invented(self):
        original = {"messages": [{"sender": "坐席", "content": "完成后请截图发到群里"}]}
        messages = [message(1, "坐席", "完成后请截图发到群里"), message(2, text="", kind="图片", code="SCREENSHOT_one")]
        self.assertEqual(screenshot_errors(original, run(messages, "completed")), [])

    def test_existing_flow_versions_are_unchanged(self):
        for version in (1, 2, 3):
            self.assertEqual(screenshot_errors(source(), run([{"seq": 1, "type": "invalid"}], "completed", version)), [])
        self.assertEqual(screenshot_errors(source(), {"messages": [{"type": "invalid"}]}), [])

    def test_v15_material_code_leak_is_reported_with_sequence(self):
        messages = [announcement(), message(22, text="", kind="图片", code="SCREENSHOT_01"),
                    message(25, text="已经添加【@Lgjaio】，把截图 SCREENSHOT_01 和【FHR888】发过去申请奖励了。")]
        errors = screenshot_errors(source(), run(messages, "completed"))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("消息 25", errors[0])
        self.assertIn("图片素材码 SCREENSHOT_01", errors[0])
        self.assertIn("不能作为角色口述内容", errors[0])

    def test_material_code_leak_checks_content_for_all_media_types_and_future_events(self):
        for kind, code in (("文本", "-"), ("图片", "SCREENSHOT_01"), ("图片+文本", "SCREENSHOT_01")):
            with self.subTest(kind=kind):
                messages = [announcement(), message(2, text="这张是【SCREENSHOT_01】。", kind=kind, code=code),
                            message(3, text="", kind="图片", code="SCREENSHOT_01")]
                errors = screenshot_errors(source(), run(messages))
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("消息 2 的角色台词包含本稿图片素材码", errors[0])

    def test_business_code_and_media_metadata_remain_valid(self):
        messages = [announcement(), message(2, text="", kind="图片", code="SCREENSHOT_01"),
                    message(3, text="截图和【FHR888】发过去了。"),
                    message(4, text="截图已经发到群里了", kind="图片+文本", code="SCREENSHOT_other")]
        messages[2]["private_note"] = "SCREENSHOT_01"
        self.assertEqual(screenshot_errors(source(), run(messages, "completed")), [])
        # Match the actual code token, not a prefix of another identifier.
        messages.append(message(5, text="SCREENSHOT_010、SCREENSHOT_unused、XSCREENSHOT_01"))
        self.assertEqual(screenshot_errors(source(), run(messages, "completed")), [])

    def test_material_code_leak_does_not_change_legacy_modes(self):
        messages = [message(1, text="SCREENSHOT_01"),
                    message(2, text="", kind="图片", code="SCREENSHOT_01")]
        for version in (1, 2, 3):
            with self.subTest(version=version):
                self.assertEqual(screenshot_errors(source(), run(messages, version=version)), [])

    def test_chronology_uses_sequence_instead_of_input_array_order(self):
        image = message(3, text="", kind="图片", code="SCREENSHOT_one")
        claim = message(2, text="我的截图已发群里了")
        errors = screenshot_errors(source(), run([image, announcement(), claim], "completed"))
        self.assertTrue(any("消息 2" in error and "没有本人" in error for error in errors), errors)

    def test_export_and_read_xlsx_preserves_media_columns(self):
        messages = [announcement(), message(2, text="", kind="图片", code="SCREENSHOT_one"),
                    message(3, text="截图已经分享了", kind="图片+文本", code="SCREENSHOT_two")]
        reread = read_source(export_xlsx(run(messages, "completed")))
        self.assertEqual([(item["type"], item["image_code"], item["image_mode"]) for item in reread["messages"]],
                         [(item["type"], item["image_code"], item["image_mode"]) for item in messages])
        self.assertIsNone(reread["messages"][1]["content"])
        self.assertEqual(reread["messages"][2]["content"], messages[2]["content"])
        self.assertEqual(screenshot_errors(source(), run(reread["messages"], "completed")), [])


if __name__ == "__main__":
    unittest.main()
