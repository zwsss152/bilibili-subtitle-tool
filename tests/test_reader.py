"""Presentation must preserve content, boundaries and timestamp ownership."""
import unittest

from src.ui.reader import parse_document


class ReaderTests(unittest.TestCase):
    def test_title_metadata_and_headings_without_markers(self):
        layout = parse_document('# 稿件名称\n来源：https://example.invalid\n整理方式：本地智能整理\n\n## 预算\n\n预算100000元，不是10000元。Hello world。\n')
        self.assertEqual(layout.title, '稿件名称')
        self.assertEqual(layout.metadata, ('来源：https://example.invalid', '整理方式：本地智能整理'))
        self.assertEqual(layout.blocks[0].style, 'heading')
        self.assertNotIn('#', layout.copy_text)
        for value in ('稿件名称', '来源：https://example.invalid', '预算100000元，不是10000元。Hello world。'):
            self.assertEqual(layout.copy_text.count(value), 1)

    def test_soft_wraps_merge_but_empty_lines_preserve_paragraphs(self):
        layout = parse_document('# 标题\n\n玩家看到\n一只怪物。\n\n下一段\n仍在讨论。')
        self.assertEqual([b.text for b in layout.blocks], ['玩家看到一只怪物。', '下一段仍在讨论。'])
        self.assertIn('一只怪物。\n\n下一段', layout.copy_text)

    def test_english_spaces_and_numbers_survive_unwrapping(self):
        layout = parse_document('Hello\nworld.\nNext sentence: 100000。\n\nEnglish spaces stay intact.', '历史稿')
        self.assertEqual(layout.blocks[0].text, 'Hello world. Next sentence: 100000。')
        self.assertIn('English spaces stay intact.', layout.copy_text)

    def test_timestamp_continuations_and_ranges(self):
        layout = parse_document('# 原稿\n平台：B站\n\n[00:00] 我认为\n这可能成立。\n\n[00:12–00:20] 第二条。')
        self.assertEqual([b.timestamp for b in layout.blocks], ['00:00', '00:12–00:20'])
        self.assertEqual(layout.blocks[0].text, '我认为这可能成立。')
        self.assertIn('[00:12–00:20]', layout.copy_text)

    def test_consecutive_timestamps_do_not_drop_or_duplicate_segments(self):
        layout = parse_document('[00:00] 第一条。\n[00:05] 第二条。\n后半句。', '原稿')
        self.assertEqual([(b.timestamp, b.text) for b in layout.blocks], [('00:00','第一条。'), ('00:05','第二条。后半句。')])

    def test_unheaded_historical_file_keeps_first_sentence(self):
        layout = parse_document('没有标题的第一句。\n\n第二段。', '历史文件名')
        self.assertEqual(layout.title, '历史文件名')
        self.assertEqual(layout.blocks[0].text, '没有标题的第一句。')

    def test_metadata_like_body_lines_are_not_relocated(self):
        layout = parse_document('# 标题\n来源：网页\n\n正文。\n\n来源：这句话是正文的一部分。')
        self.assertEqual(layout.metadata, ('来源：网页',))
        self.assertEqual(layout.blocks[-1].text, '来源：这句话是正文的一部分。')

    def test_lists_remain_separate_and_literal_hash_is_preserved(self):
        layout = parse_document('# 标题\n\n- 第一项 C#\n- 第二项\n1. 数字列表\n2. 保留顺序')
        self.assertEqual([b.text for b in layout.blocks], ['- 第一项 C#', '- 第二项', '1. 数字列表', '2. 保留顺序'])
        self.assertIn('C#', layout.copy_text)

    def test_headings_end_paragraphs_without_losing_content(self):
        layout = parse_document('# 标题\n\n第一段\n## 第二节\n第二段\n### 第三节\n第三段')
        self.assertEqual([(b.style,b.text) for b in layout.blocks], [('body','第一段'), ('heading','第二节'), ('body','第二段'), ('heading','第三节'), ('body','第三段')])

    def test_bom_crlf_long_url_and_negatives(self):
        url = 'https://example.invalid/' + 'a' * 500
        layout = parse_document(f'\ufeff# 标题\r\n来源：{url}\r\n\r\n可能不是100000，也不是10000。\r\n')
        self.assertEqual(layout.metadata, (f'来源：{url}',))
        self.assertEqual(layout.blocks[0].text, '可能不是100000，也不是10000。')


if __name__ == '__main__':
    unittest.main()
