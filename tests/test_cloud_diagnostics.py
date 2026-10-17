"""Offline error retention and redaction regression checks."""
import io
import json
import queue
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from src.core import cloud
from src.services.recognizer import worker


class CloudDiagnosticsTests(unittest.TestCase):
    def request_error(self, body, status=400):
        return urllib.error.HTTPError('https://dashscope.aliyuncs.com/api', status, 'bad',
            {'x-request-id': 'header-request-123'}, io.BytesIO(body))

    def test_400_keeps_service_details_without_echoed_credentials(self):
        records = []
        secret = 'fake-private-credential'
        body = json.dumps(dict(code='InvalidParameter', message='audio rejected '+secret,
            request_id='service-request-456', input={'audio': 'DO NOT LOG'})).encode()
        with patch.object(cloud, 'key_value', return_value=secret), patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = self.request_error(body)
            client = cloud.CloudClient(diagnostic=records.append)
            client.request_context = dict(block=39, start_seconds=4558, end_seconds=4682)
            with self.assertRaises(cloud.CloudError) as caught:
                client.recognize(b'wav')
        self.assertIn('InvalidParameter', str(caught.exception))
        self.assertIn('service-request-456', str(caught.exception))
        self.assertEqual(records[0]['http_status'], 400)
        self.assertEqual(records[0]['block'], 39)
        self.assertEqual(opener.return_value.open.call_count, 1)
        self.assertNotIn(secret, json.dumps(records)+str(caught.exception))
        self.assertNotIn('DO NOT LOG', json.dumps(records))

    def test_nested_and_non_json_responses_keep_header_request_id(self):
        for body in (b'{"error":{"code":"BadAudio","message":"invalid WAV"}}',
                     b'<html>upstream rejected audio</html>', b'[]'):
            with self.subTest(body=body), patch.object(cloud, 'key_value', return_value='fake'), patch('urllib.request.build_opener') as opener:
                records = []
                opener.return_value.open.side_effect = self.request_error(body)
                with self.assertRaises(cloud.CloudError):
                    cloud.CloudClient(diagnostic=records.append).recognize(b'wav')
                self.assertEqual(records[0]['request_id'], 'header-request-123')
                self.assertTrue(records[0]['message'])

    def test_large_body_is_bounded_and_embedded_media_redacted(self):
        message = 'data:audio/wav;base64,'+'A'*4000+'\nCookie: session=private\n'+'long description '*300
        summary, detail = cloud.service_error(dict(code='InvalidParameter', message=message), 400)
        self.assertNotIn('AAAA', summary)
        self.assertNotIn('session=private', summary)
        self.assertLess(len(detail['message']), 1700)
        self.assertIn('[truncated]', detail['message'])

    def test_transient_failure_records_each_attempt_and_network_cause(self):
        records = []
        with patch.object(cloud, 'key_value', return_value='fake'), patch('urllib.request.build_opener') as opener, patch.object(cloud.time, 'monotonic', side_effect=range(30)):
            opener.return_value.open.side_effect = urllib.error.URLError('TLS handshake failed')
            with self.assertRaisesRegex(cloud.CloudError, 'TLS handshake failed'):
                cloud.CloudClient(diagnostic=records.append).recognize(b'wav')
        self.assertEqual([r['attempt'] for r in records], [1, 2, 3])

    def test_worker_forwards_diagnostics_and_trace_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / 'audio.wav'
            audio.write_bytes(b'fake-offline-audio')
            inputs, outputs = queue.Queue(), queue.Queue()
            inputs.put(dict(identity=('job-test', 1), audio=str(audio), checkpoint=str(Path(folder)/'checkpoint.json')))
            inputs.put(None)
            with patch.object(cloud, 'key_value', return_value='fake-key'), patch.object(cloud, 'probe_duration', return_value=120), patch.object(cloud, 'find_ffmpeg', return_value='fake-ffmpeg'), patch.object(cloud, '_audio_chunk', return_value=b'wav'), patch('urllib.request.build_opener') as opener:
                opener.return_value.open.side_effect = self.request_error(b'{"code":"BadAudio","message":"decoder failed"}')
                worker(inputs, outputs, threading.Event())
            events = []
            while not outputs.empty():
                events.append(outputs.get_nowait())
            diagnostics = [e[2] for e in events if e[0] == 'diagnostic']
            self.assertEqual(diagnostics[0]['block'], 1)
            self.assertIn('decoder failed', diagnostics[0]['message'])
            self.assertIn('Traceback', diagnostics[1]['traceback'])
            self.assertIn('BadAudio', events[-1][2])
            self.assertIn('1/1', events[-1][2])
            self.assertFalse((Path(folder)/'checkpoint.json').exists())


if __name__ == '__main__':
    unittest.main()
