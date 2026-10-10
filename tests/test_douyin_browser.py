import json
import unittest
from unittest.mock import Mock, patch

from src.core import douyin, douyin_browser
from src.core.net import JobCancelled


class BrowserAcquisitionTests(unittest.TestCase):
    def record(self, video_id='123'):
        return dict(aweme_id=video_id, desc='实际视频', video=dict(duration=45000,
                    play_addr=dict(url_list=['https://v3.douyinvod.com/video.mp4'])),
                    music=dict(play_url=dict(url_list=['https://example.invalid/background.mp3'])))

    def test_requested_video_only_and_no_soundtrack_fallback(self):
        payload = dict(recommendations=[self.record('456')], aweme_detail=self.record())
        info = douyin.playback_info(douyin.find_video_record(payload, '123'), '123')
        self.assertEqual(info['title'], '实际视频')
        self.assertEqual(info['duration'], 45)
        self.assertIn('douyinvod', info['media_url'])
        with self.assertRaises(ValueError):
            douyin.playback_info(self.record('456'), '123')
        record = self.record()
        record['video'].pop('play_addr')
        with self.assertRaises(ValueError):
            douyin.playback_info(record, '123')

    def test_response_filter_never_reads_login_or_third_party_responses(self):
        self.assertTrue(douyin_browser.detail_response('https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=123'))
        for url in ('https://www.douyin.com/passport/login/',
                    'https://www.douyin.com.evil.invalid/aweme/v1/web/aweme/detail/',
                    'http://www.douyin.com/aweme/v1/web/aweme/detail/'):
            self.assertFalse(douyin_browser.detail_response(url))

    def test_browser_used_after_direct_access_failure(self):
        import yt_dlp
        expected = douyin.playback_info(self.record(), '123')
        with patch('urllib.request.urlopen', side_effect=OSError()), \
             patch.object(yt_dlp.YoutubeDL, 'extract_info', side_effect=yt_dlp.utils.DownloadError('restricted')), \
             patch.object(douyin_browser, 'fetch_browser_video', return_value=expected) as browser:
            self.assertEqual(douyin.fetch_douyin_video('123'), expected)
            browser.assert_called_once_with('123', None, None)

    def test_share_page_success_does_not_start_browser(self):
        from test_regressions import Response
        page = ('window._ROUTER_DATA = ' + json.dumps(dict(detail=self.record()))).encode()
        with patch('urllib.request.urlopen', return_value=Response(page)), \
             patch.object(douyin_browser, 'fetch_browser_video') as browser:
            self.assertEqual(douyin.fetch_douyin_video('123')['duration'], 45)
            browser.assert_not_called()

    def test_cancellation_before_launch_releases_profile_lock(self):
        with self.assertRaises(JobCancelled), patch.object(douyin_browser.mp, 'get_context') as context:
            douyin_browser.fetch_browser_video('123', cancel=lambda: True)
        context.assert_not_called()
        self.assertTrue(douyin_browser._SESSION_LOCK.acquire(blocking=False))
        douyin_browser._SESSION_LOCK.release()

    def test_result_closes_process_and_never_persists_playback_url(self):
        context = Mock()
        receiver, sender, process, stop = Mock(), Mock(), Mock(), Mock()
        context.Pipe.return_value = receiver, sender
        context.Process.return_value = process
        context.Event.return_value = stop
        expected = douyin.playback_info(self.record(), '123')
        receiver.poll.return_value = True
        receiver.recv.return_value = ('result', expected)
        process.is_alive.return_value = False
        with patch.object(douyin_browser.mp, 'get_context', return_value=context):
            self.assertEqual(douyin_browser.fetch_browser_video('123'), expected)
        stop.set.assert_called_once()
        process.close.assert_called_once()
        receiver.close.assert_called_once()

    def test_window_closed_leaves_task_retryable_and_cleans_process(self):
        context = Mock()
        receiver, sender, process = Mock(), Mock(), Mock()
        context.Pipe.return_value = receiver, sender
        context.Process.return_value = process
        receiver.poll.return_value = True
        receiver.recv.return_value = ('error', '抖音访问窗口已关闭；任务保留，可重试')
        process.is_alive.return_value = True
        with patch.object(douyin_browser.mp, 'get_context', return_value=context), \
             self.assertRaisesRegex(RuntimeError, '可重试'):
            douyin_browser.fetch_browser_video('123')
        process.terminate.assert_called_once()
        context.Event.return_value.set.assert_called_once()

    def test_cancellation_after_launch_stops_worker(self):
        context = Mock()
        receiver, sender, process = Mock(), Mock(), Mock()
        context.Pipe.return_value = receiver, sender
        context.Process.return_value = process
        calls = iter((False, True))
        process.is_alive.return_value = False
        with patch.object(douyin_browser.mp, 'get_context', return_value=context), \
             self.assertRaises(JobCancelled):
            douyin_browser.fetch_browser_video('123', cancel=lambda: next(calls))
        context.Event.return_value.set.assert_called_once()
        process.close.assert_called_once()

    def test_spawn_failure_does_not_mask_error_or_keep_profile_lock(self):
        context = Mock()
        receiver, sender, process = Mock(), Mock(), Mock()
        context.Pipe.return_value = receiver, sender
        context.Process.return_value = process
        process.pid = None
        process.start.side_effect = OSError('spawn failed')
        with patch.object(douyin_browser.mp, 'get_context', return_value=context), \
             self.assertRaisesRegex(OSError, 'spawn failed'):
            douyin_browser.fetch_browser_video('123')
        process.join.assert_not_called()
        self.assertTrue(douyin_browser._SESSION_LOCK.acquire(blocking=False))
        douyin_browser._SESSION_LOCK.release()


if __name__ == '__main__':
    unittest.main()
