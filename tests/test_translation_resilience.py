import unittest
from unittest.mock import patch
import requests
from fb_collector.services import translator
from fb_collector.services.scraper import translate_field


class TranslationResilienceTests(unittest.TestCase):
    def test_failure_keeps_successful_chunks_and_order(self):
        with patch.object(translator, 'translation_config', return_value={}), patch.object(
            translator, 'split_text', return_value=['first', 'second', 'third']
        ), patch.object(translator, 'translate_chunk', side_effect=['第一段', requests.Timeout(), '第三段']):
            result = translator.translate_to_chinese_detail('long text')
        self.assertTrue(result['text'].startswith('第一段\n'))
        self.assertTrue(result['text'].endswith('\n第三段'))
        self.assertIn('第 2 段翻译失败', result['text'])
        self.assertIn('超时', result['error'])

    def test_all_failed_has_no_fake_translation(self):
        with patch.object(translator, 'translation_config', return_value={}), patch.object(
            translator, 'translate_chunk', side_effect=requests.ConnectionError('private URL')
        ):
            result = translator.translate_to_chinese_detail('bonjour')
        self.assertEqual(result['text'], '')
        self.assertIn('无法连接', result['error'])
        self.assertNotIn('private URL', result['error'])

    def test_ocr_language_is_forwarded(self):
        values = {'ocr_text': 'bonjour', 'ocr_language': 'fra'}
        with patch('fb_collector.services.scraper.translate_to_chinese_detail', return_value={'text': '你好', 'error': ''}) as translate:
            translate_field(values, 'ocr_text', 'ocr_text_zh', 'ocr_translate_error')
        self.assertEqual(translate.call_args.kwargs['source_language'], 'fr')

    def test_explicit_source_used_for_free_fallback(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"responseData":{"translatedText":"translated"},"responseStatus":200}'
        with patch.object(translator, '_google_cooldown_until', float('inf')), patch.object(
            translator.requests, 'get', return_value=response
        ) as get, patch.object(translator.time, 'sleep'):
            result = translator.translate_chunk('unique french text', {'provider': 'free', 'source_language': 'fr'})
        self.assertEqual(result, 'translated')
        self.assertEqual(get.call_args.kwargs['params']['langpair'], 'fr|zh-CN')

    def test_http_error_is_actionable_and_hides_url(self):
        response = requests.Response()
        response.status_code = 429
        result = translator.safe_api_error(requests.HTTPError('secret URL', response=response))
        self.assertIn('额度', result)
        self.assertNotIn('secret', result)
