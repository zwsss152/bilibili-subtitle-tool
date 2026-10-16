import os
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.paths import application_paths
from src.core import cloud
from src.config import atomic_json
from src.core.links import parse_source
from src.services.scheduler import Scheduler
from src.paths import PROJECT_ROOT


class PackagingAndSettingsTests(unittest.TestCase):
    def test_catalog_lookup_filters_models_without_saving_credential(self):
        response = io.BytesIO(json.dumps(dict(data=[
            dict(id='qwen3.8-flash'), dict(id='qwen-audio-3.0-asr-flash'),
            dict(id='qwen-image-3.0'), dict(id='qwen-audio-3.0-realtime-flash'),
            dict(id='https://not-a-model')])).encode())
        with patch('urllib.request.build_opener') as opener, patch.object(cloud, 'save_credentials') as save:
            opener.return_value.open.return_value = response
            models = cloud.available_models('fake-key-never-saved')
        self.assertEqual(models['asr_model'], ['qwen-audio-3.0-asr-flash'])
        self.assertEqual(models['editor_model'], ['qwen3.8-flash'])
        save.assert_not_called()

    def test_missing_model_error_is_distinct_from_invalid_key(self):
        self.assertIn('模型', cloud._error_message(status=404))
        self.assertIn('鉴权', cloud._error_message(status=401))

    def test_frozen_paths_keep_resources_outside_writable_data(self):
        with patch.dict(os.environ, {}, clear=True):
            resources, data = application_paths(frozen=True, bundle='C:/App/_internal', local_app_data='C:/Users/test/AppData/Local')
        self.assertEqual(resources, Path('C:/App/_internal').resolve())
        self.assertEqual(data, Path('C:/Users/test/AppData/Local/BilibiliSubtitleTool').resolve())
        self.assertNotEqual(resources, data)

    def test_source_paths_preserve_existing_workspace_data(self):
        with patch.dict(os.environ, {}, clear=True):
            resources, data = application_paths(frozen=False, bundle='C:/project')
        self.assertEqual(resources, data)

    def test_explicit_data_root_must_be_absolute(self):
        with self.assertRaises(ValueError):
            application_paths(override='relative-data')

    def test_model_selection_validates_role_and_rejects_urls(self):
        with self.assertRaises(ValueError):
            cloud.normalize_models(dict(asr_model='qwen3-asr-flash-filetrans'))
        with self.assertRaises(ValueError):
            cloud.normalize_models(dict(editor_model='https://example.invalid/steal-key'))

    def test_user_selected_models_reach_each_request(self):
        payloads = []
        def transport(endpoint, payload, streaming):
            payloads.append(payload)
            return [dict(choices=[dict(finish_reason='stop', message=dict(content='{}'))])]
        client = cloud.CloudClient(transport, models=dict(editor_model='qwen-plus', review_model='qwen3.5-plus'))
        client.generate('test', '{}', dict(properties=dict(units={})))
        client.generate('test', '{}', dict(properties=dict(checks={})))
        self.assertEqual([p['model'] for p in payloads], ['qwen-plus', 'qwen3.5-plus'])

    def test_qwen3_asr_uses_its_native_payload_and_coarse_timing(self):
        payloads = []
        def transport(endpoint, payload, streaming):
            payloads.append(payload)
            return [dict(output=dict(choices=[dict(finish_reason='stop', message=dict(content=[dict(text='测试原话。')]))]))]
        client = cloud.CloudClient(transport, models=dict(asr_model='qwen3-asr-flash'))
        result = client.recognize(b'wav', 'context')
        self.assertEqual(result['text'], '测试原话。')
        self.assertFalse(result['precise'])
        self.assertIn('audio', payloads[0]['input']['messages'][-1]['content'][0])
        self.assertEqual(payloads[0]['model'], 'qwen3-asr-flash')

    def test_queued_models_are_persisted_and_immune_to_settings_changes(self):
        root = PROJECT_ROOT / 'tests' / '.artifacts'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder, patch.object(cloud, 'SETTINGS', Path(folder) / 'settings.json'), patch('src.services.scheduler.configured', return_value=True):
            state = Path(folder) / 'queue.json'
            atomic_json(cloud.SETTINGS, dict(asr_model='fun-asr-flash'))
            scheduler = Scheduler(state_path=state, autostart=False)
            scheduler.enqueue('BV17x411w7KC', 'topic', '')
            atomic_json(cloud.SETTINGS, dict(asr_model='qwen3-asr-flash'))
            self.assertEqual(scheduler.jobs[0].cloud_models['asr_model'], 'fun-asr-flash')
            restored = Scheduler(state_path=state, autostart=False)
            self.assertEqual(restored.jobs[0].cloud_models['asr_model'], 'fun-asr-flash')

    def test_model_change_invalidates_audio_checkpoint(self):
        root = PROJECT_ROOT / 'tests' / '.artifacts'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder:
            audio, checkpoint = Path(folder) / 'audio.wav', Path(folder) / 'state.json'
            audio.write_bytes(b'fixture')
            class Fake:
                asr_model = 'first-model'
                calls = 0
                def start(self, cancel=None): pass
                def recognize(self, *args):
                    self.calls += 1
                    return dict(text='原话。', precise=False, usage={})
            client = Fake()
            with patch.object(cloud, 'probe_duration', return_value=1), patch.object(cloud, '_audio_chunk', return_value=b'wav'):
                cloud.transcribe(audio, checkpoint, client=client)
                client.asr_model = 'second-model'
                cloud.transcribe(audio, checkpoint, client=client)
            self.assertEqual(client.calls, 2)
