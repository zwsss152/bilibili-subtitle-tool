"""Cloud integrity/lifecycle tests with fake HTTP, no paid calls or real keys."""
import json
import io
import itertools
import wave
import urllib.error
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.config import atomic_json
from src.paths import PROJECT_ROOT
from src.core import cloud, chinese_review, download, reading
from src.core.links import parse_source
from src.core.net import JobCancelled
from src.services.scheduler import Scheduler
from src.services.tasks import Task


def event(text, words=None, end=True, sid=1):
    return dict(output=dict(text=text, sentence=dict(sentence_id=sid, sentence_end=end,
        begin_time=0, end_time=1000, text=text, words=words or [])), usage=dict(duration=1))


class CloudTests(unittest.TestCase):
    def setUp(self):
        artifacts = PROJECT_ROOT / 'tests' / '.artifacts'
        artifacts.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='cloud-',dir=artifacts)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        for name,path in [('DATA',self.root),('SECRET',self.root/'key.dpapi'),('SETTINGS',self.root/'settings.json')]:
            item=patch.object(cloud,name,path);item.start();self.addCleanup(item.stop)

    def test_windows_key_round_trip_never_stores_plaintext(self):
        key='fake-test-key-never-used-online'
        cloud.save_credentials(key,'test-workspace')
        self.assertNotIn(key.encode(),cloud.SECRET.read_bytes())
        self.assertEqual(cloud.key_value(),key)
        cloud.save_credentials('', '')
        self.assertEqual(cloud.key_value(),key)
        self.assertNotIn(key,cloud.SETTINGS.read_text())

    def test_workspace_rejects_host_or_path_injection(self):
        for value in ['a.example.com','../x','x/secret','a?query']:
            with self.assertRaises(ValueError):cloud.save_credentials('',value)

    def test_audio_is_direct_base64_without_cookie_or_source_url(self):
        seen=[]
        def transport(endpoint,payload,stream):
            seen.append((endpoint,payload,stream))
            return [event('预算100000元。',[dict(begin_time=0,end_time=1000,text='预算100000元',punctuation='。',fixed=True)])]
        result=cloud.CloudClient(transport).recognize(b'fake-wav','游戏策划')
        self.assertTrue(result['precise'])
        self.assertEqual(result['segments'][0]['text'],'预算100000元。')
        message=seen[0][1]['input']['messages'][-1]['content'][0]
        self.assertTrue(message['input_audio']['data'].startswith('data:audio/wav;base64,'))
        self.assertNotIn('Cookie',json.dumps(seen))
        self.assertFalse(seen[0][2])

    def test_stream_cumulative_text_is_not_duplicated(self):
        first=dict(begin_time=0,end_time=500,text='真实B',punctuation='，',fixed=True)
        second=dict(begin_time=500,end_time=1000,text='数字7',punctuation='。',fixed=True)
        result=cloud.CloudClient(lambda *a:[event('真实B，',[first]),event('真实B，数字7。',[second],sid=2)]).recognize(b'wav')
        self.assertEqual(''.join(s['text'] for s in result['segments']),'真实B，数字7。')
        self.assertTrue(result['precise'])

    def test_partial_word_timestamps_do_not_drop_accumulated_text(self):
        result=cloud.CloudClient(lambda *a:[event('第一句。第二句。',[
            dict(begin_time=500,end_time=1000,text='第二句',punctuation='。')])]).recognize(b'wav')
        self.assertEqual(result['text'],'第一句。第二句。')
        self.assertFalse(result['precise'])
        self.assertEqual(result['segments'],[])

    def test_empty_recognition_does_not_succeed(self):
        with self.assertRaises(cloud.CloudError):cloud.CloudClient(lambda *a:[event('')]).recognize(b'wav')

    def test_writer_and_auditor_use_different_models(self):
        models=[]
        def transport(endpoint,payload,stream):
            models.append(payload['model'])
            return [dict(choices=[dict(finish_reason='stop',message={'content':'{"ok":true}'})],usage={})]
        client=cloud.CloudClient(transport)
        client.generate('JSON','{}',{'properties':{'units':{}}})
        client.generate('JSON','{}',{'properties':{'checks':{}}})
        self.assertEqual(models,[cloud.EDITOR_MODEL,cloud.REVIEW_MODEL])

    def test_truncated_writer_is_rejected(self):
        client=cloud.CloudClient(lambda *a:[dict(choices=[dict(finish_reason='length',message={'content':'{}'})])])
        with self.assertRaises(cloud.CloudError):client.generate('JSON','{}',{'properties':{}})

    def test_cancel_before_request_never_calls_transport(self):
        called=[]
        with self.assertRaises(JobCancelled):
            cloud.CloudClient(lambda *a:called.append(a)).generate('JSON','{}',{},lambda:True)
        self.assertEqual(called,[])

    def test_audio_resume_skips_successful_chunks_and_owns_overlap(self):
        audio=self.root/'audio.wav';audio.write_bytes(b'consistent-audio')
        checkpoint=self.root/'checkpoint.json'
        class Client:
            calls=0
            def start(self,cancel=None):pass
            def recognize(self,wav,context='',cancel=None):
                self.calls+=1
                if self.calls==2:raise cloud.CloudError('模拟断网')
                return dict(text='第一块',precise=True,usage={},segments=[dict(start=0,end=1,text='第一块')])
        client=Client()
        with patch.object(cloud,'probe_duration',return_value=240),patch.object(cloud,'_audio_chunk',return_value=b'wav'):
            with self.assertRaises(cloud.CloudError):cloud.transcribe(audio,checkpoint,client=client)
            self.assertEqual(len(json.loads(checkpoint.read_text(encoding='utf-8'))['blocks']),1)
            # Second request starts at 118; its first overlap word belongs to
            # the preceding chunk, and its later word belongs to this one.
            class Resume(Client):
                calls=0
                def recognize(self,*a):
                    self.calls+=1
                    return dict(text='重叠第二块',precise=True,usage={},segments=[
                        dict(start=0,end=1,text='重叠'),dict(start=2,end=3,text='第二块')])
            resumed=Resume()
            result=cloud.transcribe(audio,checkpoint,client=resumed)
            self.assertEqual(resumed.calls,1)
            self.assertEqual([s['text'] for s in result['segments']],['第一块','第二块'])
            self.assertEqual(result['segments'][1]['start'],120)

    def test_coarse_response_retries_without_overlap_preserving_full_text(self):
        audio=self.root/'audio.wav';audio.write_bytes(b'audio')
        class Client:
            def start(self,cancel=None):pass
            def recognize(self,*a):return dict(text='完整句子。',precise=False,usage={})
        with patch.object(cloud,'probe_duration',return_value=121),patch.object(cloud,'_audio_chunk',return_value=b'wav') as run:
            result=cloud.transcribe(audio,self.root/'state.json',client=Client())
        self.assertEqual([s['text'] for s in result['segments']],['完整句子。','完整句子。'])
        self.assertTrue(all(s['precision']=='chunk' for s in result['segments']))
        self.assertEqual(run.call_count,4)
        self.assertEqual(result['segments'][1]['start'],120)

    def test_changed_audio_invalidates_resume(self):
        audio=self.root/'audio.wav';audio.write_bytes(b'first')
        class Client:
            calls=0
            def start(self,cancel=None):pass
            def recognize(self,*a):
                self.calls+=1
                return dict(text='原话。',precise=False,usage={})
        client=Client()
        with patch.object(cloud,'probe_duration',return_value=1),patch.object(cloud,'_audio_chunk',return_value=b'wav'):
            cloud.transcribe(audio,self.root/'state.json',client=client)
            audio.write_bytes(b'different')
            cloud.transcribe(audio,self.root/'state.json',client=client)
        self.assertEqual(client.calls,2)

    def test_bad_characters_are_retried_once_and_not_saved_as_success(self):
        audio=self.root/'audio.wav';audio.write_bytes(b'audio')
        class Client:
            calls=0
            def start(self,cancel=None):pass
            def recognize(self,*a):
                self.calls+=1
                return dict(text='缺损\ufffd内容',precise=False,usage={})
        client=Client()
        with patch.object(cloud,'probe_duration',return_value=1),patch.object(cloud,'_audio_chunk',return_value=b'wav'):
            with self.assertRaises(cloud.CloudError):cloud.transcribe(audio,self.root/'state.json',client=client)
        self.assertEqual(client.calls,2)
        self.assertFalse((self.root/'state.json').exists())

    def test_mode_is_captured_and_restored_per_task(self):
        scheduler=Scheduler(state_path=self.root/'queue.json',autostart=False)
        scheduler.enqueue('BV17x411w7KC','主题','','local')
        with patch('src.services.scheduler.configured',return_value=True):
            scheduler.enqueue('BV17x411w7KC','主题','','cloud')
        self.assertEqual([j.engine for j in scheduler.jobs],['local','cloud'])
        restored=Scheduler(state_path=self.root/'queue.json',autostart=False)
        self.assertEqual([j.engine for j in restored.jobs],['local','cloud'])
        legacy=scheduler.jobs[0].to_dict();legacy.pop('engine')
        self.assertEqual(Task.from_dict(legacy).engine,'local')

    def test_cloud_without_key_does_not_enqueue_or_download(self):
        scheduler=Scheduler(state_path=self.root/'queue.json',autostart=False)
        with patch('src.services.scheduler.configured',return_value=False):
            added,_,errors=scheduler.enqueue('BV17x411w7KC','主题','','cloud')
        self.assertEqual(added,0)
        self.assertTrue(errors)
        self.assertFalse(scheduler.jobs)

    def test_checkpoint_path_cannot_escape_and_delete_is_task_owned(self):
        first=cloud.checkpoint_path('../untrusted',1)
        second=cloud.checkpoint_path('other',1)
        first.parent.mkdir()
        first.write_text('first');second.write_text('second')
        self.assertEqual(first.resolve().parent,(self.root/'cloud-asr').resolve())
        cloud.discard_checkpoints('../untrusted')
        self.assertFalse(first.exists());self.assertTrue(second.exists())

    def test_byte_budget_rejects_unknown_length_growth(self):
        cache=self.root/'cache';cache.mkdir()
        (cache/'old').write_bytes(b'x'*20)
        with patch.object(download,'ASR_CACHE_DIR',cache),patch.object(download,'CACHE_LIMIT',25):
            with self.assertRaises(RuntimeError):download.check_cache_budget(cache/'new',6)
            download.check_cache_budget(cache/'new',5)

    def test_local_anomaly_is_acoustically_replaced_not_regex_deleted(self):
        original='我们的老哥Ｂ从业经验很久Ｂ每天研究技能Ｂ战斗节奏。'
        candidate='我们的老哥，从业经验很久，每天研究技能，战斗节奏。'
        selected,reason=chinese_review.choose_reading(original,candidate,20)
        self.assertEqual(selected,candidate)
        self.assertIn('声学复核',reason)
        self.assertFalse(chinese_review.damaged('B站有7个视频，预算100000元。'))

    def test_local_severe_truncation_can_restore_matching_audio_reading(self):
        original='把这句话转移到关卡设计上就是Ｂ'
        candidate='对关卡的理解只停留在摆怪，现在回想起来十分粗陋。他教我从根本原子层面去拆解并解决问题，把这句话转移到关卡设计上就是。'
        self.assertEqual(chinese_review.choose_reading(original,candidate,28)[0],candidate)
        self.assertEqual(chinese_review.choose_reading('预算100000元。','预算10000元。',10)[0],'预算100000元。')

    def test_api_failure_does_not_get_disguised_as_successful_layout(self):
        class Broken:
            engine='cloud'
            def generate(self,*a):raise cloud.CloudError('余额不足')
        with self.assertRaises(cloud.CloudError):
            reading.organize_chunk([dict(id=0,start='00:00',text='预算100000元。')],Broken())

    def test_word_level_numbers_are_not_split_by_paragraph_join(self):
        from src.core.textout import build_paragraphs
        words=[dict(start=i,end=i+.5,text=t) for i,t in enumerate(['预算','1','0','0','0','0','0','元。','Hello','world。'])]
        text=build_paragraphs(words)[0][1]
        self.assertIn('100000元',text)
        self.assertIn('Hello world',text)

    def test_ambiguous_pronoun_must_not_be_guessed_as_a_name(self):
        self.assertTrue(reading.guard('完美契合了他质问、对峙的情绪。内森背后的道具形状复杂。',
            '场景设计完美契合了内森质问、对峙的情绪。内森背后的道具形状复杂。'))

    def test_chinese_four_before_daily_is_not_a_quantity(self):
        self.assertEqual(chinese_review._numbers('我一天天奔四日常焦虑。'),[])
        self.assertEqual(chinese_review._numbers('四十五加的年纪，45岁。'),['45','45'])

    def test_isolated_homophone_does_not_overwrite_correct_first_reading(self):
        self.assertEqual(chinese_review.choose_reading('关卡策划不是一个笼统的摆怪的人。',
            '关卡策划不是一个笼统的百怪的人。')[0],'关卡策划不是一个笼统的摆怪的人。')

    def test_cloud_schema_is_explicit_in_messages(self):
        seen=[]
        def transport(endpoint,payload,stream):
            seen.append(payload)
            return [dict(choices=[dict(finish_reason='stop',message={'content':'{}'})])]
        schema={'properties':{'units':{'type':'array'}}}
        cloud.CloudClient(transport).generate('JSON','{}',schema)
        self.assertIn(json.dumps(schema,ensure_ascii=False),seen[0]['messages'][0]['content'])

    def test_cloud_word_boundaries_preserve_real_english_spacing(self):
        from src.core.textout import build_paragraphs
        words=[dict(begin_time=i*100,end_time=(i+1)*100,text=t,fixed=True) for i,t in enumerate(['AR','PG','Hello','world','1','0','0','0','0','0'])]
        result=cloud.CloudClient(lambda *a:[event('ARPG Hello world 100000',words)]).recognize(b'wav')
        self.assertTrue(result['precise'])
        self.assertEqual(build_paragraphs(result['segments'],punctuate=False)[0][1],'ARPG Hello world 100000')

    def test_http_auth_error_is_sanitized_and_not_retried(self):
        error=urllib.error.HTTPError('https://example.invalid',401,'Unauthorized',{},
            io.BytesIO(b'{"code":"InvalidApiKey","message":"SECRET-MUST-NOT-LEAK"}'))
        with patch.object(cloud,'key_value',return_value='fake'),patch.object(cloud.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=error
            with self.assertRaises(cloud.CloudError) as caught:
                cloud.CloudClient().generate('JSON','{}',{})
            self.assertNotIn('SECRET',str(caught.exception))
            self.assertEqual(opener.return_value.open.call_count,1)

    def test_http_rate_limit_retries_then_succeeds(self):
        error=urllib.error.HTTPError('https://example.invalid',429,'Limited',{},io.BytesIO(b'{}'))
        response=io.BytesIO(b'{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}')
        response.headers={'Content-Type':'application/json'}
        with patch.object(cloud,'key_value',return_value='fake'),patch.object(cloud.urllib.request,'build_opener') as opener,patch.object(cloud.time,'monotonic',side_effect=itertools.count(0,10)):
            opener.return_value.open.side_effect=[error,response]
            self.assertEqual(cloud.CloudClient().generate('JSON','{}',{}),{})
            self.assertEqual(opener.return_value.open.call_count,2)

    def test_invalid_word_timing_is_not_saved_as_success(self):
        audio=self.root/'audio.wav';audio.write_bytes(b'audio')
        class Client:
            def start(self,cancel=None):pass
            def recognize(self,*a):
                return dict(text='内容',precise=True,usage={},segments=[dict(start=-1,end=1,text='内容')])
        with patch.object(cloud,'probe_duration',return_value=1),patch.object(cloud,'_audio_chunk',return_value=b'wav'):
            with self.assertRaises(cloud.CloudError):cloud.transcribe(audio,self.root/'state.json',client=Client())
        self.assertFalse((self.root/'state.json').exists())

    def test_unfinished_stream_is_not_accepted(self):
        with self.assertRaises(cloud.CloudError):
            cloud.CloudClient(lambda *a:[event('尚未结束',end=False)]).recognize(b'wav')

    def test_last_utterance_usage_is_not_mistaken_for_truncated_audio(self):
        audio=self.root/'audio.wav';audio.write_bytes(b'audio')
        class Client:
            def start(self,cancel=None):pass
            def recognize(self,*a):
                return dict(text='最后一句。',precise=True,usage={'usage':{'duration':30}},
                            segments=[dict(start=20,end=30,text='最后一句。')])
        with patch.object(cloud,'probe_duration',return_value=60),patch.object(cloud,'_audio_chunk',return_value=b'wav'):
            result=cloud.transcribe(audio,self.root/'state.json',client=Client())
        self.assertTrue((self.root/'state.json').exists())
        self.assertEqual(result['segments'][0]['text'],'最后一句。')
        self.assertEqual(result['recognition']['duration'],60)

    def test_shifted_boundary_word_is_neither_dropped_nor_duplicated(self):
        previous=[dict(start=119.7,end=120.4,text='关键路径',owned=False)]
        output,_=cloud._own_words([dict(start=1.5,end=2.2,text='关键路径')],118,120,240,previous)
        self.assertEqual([s['text'] for s in output],['关键路径'])
        previous[0].update(start=119.5,end=120.2,owned=True)
        output,_=cloud._own_words([dict(start=1.7,end=2.4,text='关键路径')],118,120,240,previous)
        self.assertEqual(output,[])

    def test_coarse_core_retry_rejects_invalid_usage(self):
        audio=self.root/'audio.wav';audio.write_bytes(b'audio')
        class Client:
            calls=0
            def start(self,cancel=None):pass
            def recognize(self,*a):
                self.calls+=1
                return dict(text='不完整内容。',precise=False,
                            usage={'usage':{'duration':122 if self.calls==1 else -1}})
        with patch.object(cloud,'probe_duration',return_value=121),patch.object(cloud,'_audio_chunk',return_value=b'wav'):
            with self.assertRaises(cloud.CloudError):cloud.transcribe(audio,self.root/'state.json',client=Client())
        self.assertFalse((self.root/'state.json').exists())

    def test_pcm_short_read_cannot_reach_cloud_client(self):
        audio=self.root/'audio.wav';audio.write_bytes(b'audio')
        client=cloud.CloudClient(lambda *a:self.fail('短读音频不得上传'))
        with patch.object(cloud,'probe_duration',return_value=60),patch.object(cloud,'_run',return_value=b'\x00'*32000):
            with self.assertRaises(cloud.CloudError):cloud.transcribe(audio,self.root/'state.json',client=client)
        self.assertFalse((self.root/'state.json').exists())

    def test_audio_chunk_has_exact_sized_wav_header(self):
        with patch.object(cloud,'_run',return_value=b'\x00'*32000):
            wav=cloud._audio_chunk('audio',0,1,'ffmpeg')
        with wave.open(io.BytesIO(wav)) as source:
            self.assertEqual(source.getnframes(),16000)
            self.assertEqual(source.getframerate(),16000)
            self.assertEqual(source.readframes(16000),b'\x00'*32000)
        self.assertEqual(int.from_bytes(wav[4:8],'little'),len(wav)-8)

    def test_truncated_json_response_cannot_be_saved_as_final(self):
        response=io.BytesIO(b'{"output":{"text":"partial')
        response.headers={'Content-Type':'application/json'}
        with patch.object(cloud,'key_value',return_value='fake'),patch.object(cloud.urllib.request,'build_opener') as opener:
            opener.return_value.open.return_value=response
            with self.assertRaises(cloud.CloudError):cloud.CloudClient().recognize(b'wav')

    def test_same_spoken_word_repeated_at_distinct_times_is_kept(self):
        previous=[dict(start=119.1,end=119.5,text='我们',owned=True)]
        output,_=cloud._own_words([dict(start=2,end=2.3,text='我们')],118,120,240,previous)
        self.assertEqual([s['text'] for s in output],['我们'])


if __name__=='__main__':unittest.main()
