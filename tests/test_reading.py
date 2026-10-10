"""Two-file lifecycle tests run only in a verified project-local fixture root."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from src.config import atomic_json
from src.paths import PROJECT_ROOT
from src.core import documents, library, library_migration, reading, rename, textout, topics
from src.core import reading as written
from src.core.links import parse_source
from src.services import reading_service
from src.services.scheduler import Scheduler
from src.services.tasks import Task, Page


class Editor:
    def __init__(self, hook=None):
        self.hook = hook
        self.calls = 0
    def start(self, cancel=None):
        pass
    def close(self):
        pass
    def generate(self, system, content, schema, cancel=None):
        self.calls += 1
        source = json.loads(content)
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        if 'units' in source:
            return dict(units=[dict(id=u['id'], information_points=[u['text']], heading='讨论内容' if i==0 else '', text=u['text'])
                               for i,u in enumerate(source['units'])])
        return dict(checks=[dict(id=u['id'],accepted=True,reason='信息保留') for u in source])


class ReadingTests(unittest.TestCase):
    def setUp(self):
        artifacts = PROJECT_ROOT / 'tests' / '.artifacts'
        artifacts.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='reading-', dir=artifacts)
        self.root = Path(self.temp.name).resolve()
        assert self.root.is_relative_to(artifacts.resolve())
        self.addCleanup(self.temp.cleanup)
        self.output, self.records = self.root/'output', self.root/'records'
        for module in (documents, library, library_migration, rename, reading_service):
            for name in ('OUTPUT_ROOT','RECORDS_DIR'):
                if hasattr(module,name):
                    p=patch.object(module,name,self.output if name=='OUTPUT_ROOT' else self.records)
                    p.start();self.addCleanup(p.stop)
        for module in (rename,library_migration):
            p=patch.object(module,'_folder',return_value=self.root/module.__name__.split('.')[-1])
            p.start();self.addCleanup(p.stop)
        self.state = self.root/'queue.json'
        self.scheduler = Scheduler(state_path=self.state,autostart=False)

    def fixture(self, count=1, units=2):
        task = Task.create(parse_source('BV17x411w7KC'),'主题')
        task.title='原名称'
        task.output_dir=str((self.output/library.ORIGINAL/'主题').relative_to(PROJECT_ROOT))
        for number in range(1,count+1):
            txt,raw = textout.save_result(PROJECT_ROOT/task.output_dir,'原名称','https://example.invalid',
                'B站','测试',[dict(start=i*10,end=i*10+5,text=f'第{i}段预算100000元，不是10000元。Hello world。')
                              for i in range(units)],f'p{number}')
            task.pages.append(Page(number,'原名称','https://example.invalid',status='完成',progress=100,
                txt=str(Path(txt).relative_to(PROJECT_ROOT)),raw=str(Path(raw).relative_to(PROJECT_ROOT))))
        task.status='完成'
        self.scheduler.jobs=[task]
        self.scheduler._persist()
        return task

    def run_draft(self, job, editor=None, local=False):
        service=self.scheduler.reading_service
        service.enqueue((job.jid,job.pages[0].number))
        with patch.object(reading,'enabled',return_value=local),patch.object(reading_service,'resources_ready',return_value=True):
            while service.has_work():service.step(editor or Editor())

    def test_both_topics_and_one_library_entry_per_document(self):
        folder,existed=topics.create_topic('新主题',self.output)
        self.assertFalse(existed)
        self.assertEqual(folder,self.output/library.ORIGINAL/'新主题')
        self.assertTrue((self.output/library.READING/'新主题').is_dir())
        job=self.fixture()
        self.run_draft(job)
        self.assertEqual(len(library.originals()),1)
        self.assertEqual(library.topics(),['主题','新主题'])

    def test_fallback_exports_same_filename_and_keeps_original_bytes(self):
        job=self.fixture()
        original=PROJECT_ROOT/job.pages[0].txt
        before=original.read_bytes()
        self.run_draft(job)
        draft=PROJECT_ROOT/job.pages[0].reading
        self.assertEqual(draft.name,original.name)
        self.assertEqual(draft.parent.parent.name,library.READING)
        self.assertIn('100000',draft.read_text(encoding='utf-8'))
        self.assertIn('Hello world',draft.read_text(encoding='utf-8'))
        self.assertNotIn('[00:',draft.read_text(encoding='utf-8'))
        self.assertEqual(original.read_bytes(),before)
        self.assertEqual(job.status,'完成')
        self.assertEqual(job.pages[0].reading_status,'原文排版')

    def test_completed_local_draft_not_repeated_and_rename_delete_both(self):
        job=self.fixture()
        editor=Editor()
        self.run_draft(job,editor,True)
        calls=editor.calls
        self.run_draft(job,editor,True)
        self.assertEqual(editor.calls,calls)
        old=[PROJECT_ROOT/job.pages[0].txt,PROJECT_ROOT/job.pages[0].reading]
        self.scheduler.rename_transcript(job.jid,'改名')
        new=[PROJECT_ROOT/job.pages[0].txt,PROJECT_ROOT/job.pages[0].reading]
        for path in new:
            self.assertEqual(path.name,'改名.txt')
            self.assertTrue(path.read_text(encoding='utf-8').startswith('# 改名\n'))
        self.assertTrue(all(not p.exists() for p in old))
        self.run_draft(job,editor,True)
        self.assertEqual(editor.calls,calls)
        self.scheduler.remove({job.jid})
        self.assertTrue(all(not p.exists() for p in new))
        self.assertFalse(documents.record_path(new[0]).exists())

    def test_reading_conflict_prevents_original_rename(self):
        job=self.fixture()
        self.run_draft(job)
        draft=PROJECT_ROOT/job.pages[0].reading
        collision=draft.with_name('冲突.txt')
        collision.write_text('另一份稿件',encoding='utf-8')
        before=job.to_dict()
        with self.assertRaises(ValueError):self.scheduler.rename_transcript(job.jid,'冲突')
        self.assertEqual(job.to_dict(),before)
        self.assertEqual(collision.read_text(encoding='utf-8'),'另一份稿件')

    def test_rename_during_generation_writes_only_new_path(self):
        job=self.fixture()
        old=library.reading_path(PROJECT_ROOT/job.pages[0].txt)
        self.run_draft(job,Editor(lambda:self.scheduler.rename_transcript(job.jid,'新名')),True)
        self.assertFalse(old.exists())
        self.assertEqual(Path(job.pages[0].reading).name,'新名.txt')
        self.assertTrue((PROJECT_ROOT/job.pages[0].reading).read_text(encoding='utf-8').startswith('# 新名\n'))

    def test_delete_during_generation_cannot_resurrect_files(self):
        job=self.fixture()
        original=PROJECT_ROOT/job.pages[0].txt
        draft=library.reading_path(original)
        self.run_draft(job,Editor(lambda:self.scheduler.remove({job.jid})),True)
        self.assertFalse(original.exists())
        self.assertFalse(draft.exists())
        self.assertFalse(self.scheduler.jobs)

    def test_external_original_change_discards_inflight_result(self):
        job=self.fixture()
        original=PROJECT_ROOT/job.pages[0].txt
        def change():
            original.write_text(original.read_text(encoding='utf-8').replace('100000','200000'),encoding='utf-8')
        self.run_draft(job,Editor(change),True)
        result=(PROJECT_ROOT/job.pages[0].reading).read_text(encoding='utf-8')
        self.assertIn('200000',result)
        self.assertNotIn('100000',result)

    def test_crash_after_txt_install_before_record_commit_recovers(self):
        job=self.fixture()
        service=self.scheduler.reading_service
        service.enqueue((job.jid,1))
        original_atomic=reading_service.atomic_json
        writes=0
        def fail(path,data):
            nonlocal writes
            writes+=1
            if writes==2:raise SystemExit('模拟写入 TXT 后断电')
            return original_atomic(path,data)
        with patch.object(reading,'enabled',return_value=False),patch.object(reading_service,'atomic_json',side_effect=fail):
            with self.assertRaises(SystemExit):service.step(Editor())
        draft=library.reading_path(PROJECT_ROOT/job.pages[0].txt)
        self.assertTrue(draft.exists())
        self.run_draft(job)
        self.assertEqual(job.pages[0].reading_status,'原文排版')

    def test_old_library_migration_preserves_bytes_queue_and_record(self):
        job=self.fixture()
        current=PROJECT_ROOT/job.pages[0].txt
        legacy=self.output/'主题'/current.name
        legacy.parent.mkdir(parents=True)
        current.rename(legacy)
        documents.record_path(current).rename(documents.record_path(legacy))
        job.pages[0].txt=str(legacy.relative_to(PROJECT_ROOT))
        job.pages[0].raw=str(documents.record_path(legacy).relative_to(PROJECT_ROOT))
        job.output_dir=str(legacy.parent.relative_to(PROJECT_ROOT))
        self.scheduler._persist()
        before=legacy.read_bytes()
        library_migration.migrate_library(self.state,root=self.output)
        self.assertFalse(legacy.exists())
        self.assertEqual(current.read_bytes(),before)
        restored=Scheduler(state_path=self.state,autostart=False)
        self.assertEqual(restored.jobs[0].status,'完成')
        self.assertEqual(restored.jobs[0].pages[0].txt,str(current.relative_to(PROJECT_ROOT)))
        self.assertTrue(documents.record_path(current).exists())
        self.assertTrue(list((self.root/'library_migration').glob('backup-*.json')))

    def test_migration_interruption_rolls_back_then_retries(self):
        legacy=self.output/'主题'/'旧稿.txt'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('# 旧稿\n来源：https://example.invalid\n\n[00:00] 预算100000元\n',encoding='utf-8')
        original_apply=rename.apply
        def crash(data):
            original_apply(data)
            raise SystemExit('模拟队列提交前退出')
        with patch.object(rename,'apply',side_effect=crash):
            with self.assertRaises(SystemExit):library_migration.migrate_library(self.state,root=self.output)
        self.assertFalse(legacy.exists())
        library_migration.migrate_library(self.state,root=self.output)
        self.assertTrue((self.output/library.ORIGINAL/'主题'/'旧稿.txt').exists())

    def test_guards_and_missing_ids_fall_back_to_full_original(self):
        for source,candidate in [('100000元','10000元'),('Hello world','Helloworld'),
                                 ('不一定成功','一定成功'),('可能发生','一定发生'),('他说“保留例子”','他说“省略例子”'),
                                 ('80%','80'),('我觉得可以','可以'),('原来的剧形匹配不了','原来的剧情匹配不了')]:
            self.assertTrue(reading.guard(source,candidate))
        class Missing(Editor):
            def generate(self,*args,**kwargs):return {'units':[]}
        source=[dict(id=0,start='00:00',text='例如，预算100000元，不是10000元。')]
        result=reading.organize_chunk(source,Missing())
        self.assertEqual(result[0]['text'],source[0]['text'])
        self.assertEqual(result[0]['mode'],'原文排版')

    def test_source_fingerprint_ignores_only_title(self):
        document=dict(text='# 名称\n来源：https://example.invalid\n\n[00:00] Hello world 100000\n',url='https://example.invalid')
        digest=reading.fingerprint(document)
        renamed={**document,'text':document['text'].replace('# 名称','# 改名')}
        self.assertEqual(digest,reading.fingerprint(renamed))
        changed={**document,'text':document['text'].replace('100000','10000')}
        self.assertNotEqual(digest,reading.fingerprint(changed))

    def test_written_equivalents_keep_opinion_and_uncertainty(self):
        source='我觉得这个方法可能不适合小团队。比如说，预算是100000元。'
        candidate='我认为这个方法可能不适合小团队。例如，预算是100000元。'
        self.assertEqual(written.guard(source,candidate),'')
        self.assertTrue(written.guard(source,candidate.replace('我认为','')))
        self.assertTrue(written.guard(source,candidate.replace('可能','肯定')))
        self.assertTrue(written.guard(source,candidate.replace('100000','10000')))

    def test_rejected_group_can_repair_one_unit_without_moving_facts(self):
        class Repair(Editor):
            def generate(self,system,content,schema,cancel=None):
                data=json.loads(content)
                if isinstance(data,dict):
                    self.calls+=1
                    if len(data['units'])>1:return {'units':[]}
                return super().generate(system,content,schema,cancel)
        source=[dict(id=i,start=str(i),text=f'第{i}段预算100000元，不是10000元。') for i in range(2)]
        result=written.organize_chunk(source,Repair())
        self.assertEqual([b['id'] for b in result],[0,1])
        self.assertEqual([b['text'] for b in result],[u['text'] for u in source])
        self.assertTrue(all(b['mode']=='本地智能整理' for b in result))

    def test_copied_oral_draft_gets_one_bounded_written_retry(self):
        original='大家好，欢迎回来。我认为预算是100000元，不是10000元。'
        class Written(Editor):
            def generate(self,system,content,schema,cancel=None):
                result=super().generate(system,content,schema,cancel)
                if 'units' in result and '【本段重写】' in system:
                    result['units'][0]['text']='我认为预算是100000元，不是10000元。'
                return result
        result=written.organize_chunk([dict(id=0,start='00:00',text=original)],Written())
        self.assertEqual(result[0]['text'],'我认为预算是100000元，不是10000元。')
        self.assertEqual(result[0]['mode'],'本地智能整理')

    def test_old_policy_completed_draft_survives_unavailable_new_policy(self):
        job=self.fixture()
        self.run_draft(job,Editor(),True)
        source=PROJECT_ROOT/job.pages[0].txt
        document=textout.read_document(source)
        document['raw']['reading']['version']=reading.VERSION-1
        atomic_json(documents.record_path(source),document['raw'])
        before=library.reading_path(source).read_bytes()
        self.run_draft(job,Editor(),False)
        self.assertEqual(library.reading_path(source).read_bytes(),before)
        self.assertEqual(textout.read_document(source)['raw']['reading']['version'],reading.VERSION-1)

    def test_cloud_pending_reading_resumes_without_enabling_history_uploads(self):
        job=self.fixture();job.engine='cloud'
        self.scheduler.request_reading(PROJECT_ROOT / job.pages[0].txt)
        self.scheduler.reading_service = reading_service.ReadingService(self.scheduler)
        self.scheduler.reading_service.scan()
        self.assertTrue(self.scheduler.reading_service.has_work())
        self.scheduler.cloud_editor=Editor()
        with patch.object(reading_service,'configured',return_value=True):
            while self.scheduler.reading_service.has_work():
                self.scheduler.reading_service.step(Editor())
        self.assertEqual(job.pages[0].reading_status,'完成')
        self.assertEqual(textout.read_document(PROJECT_ROOT/job.pages[0].txt)['raw']['reading']['strategy'],'cloud')

    def test_unrequested_original_stays_unorganized_after_restart(self):
        job = self.fixture()
        self.scheduler.reading_service.scan()
        self.assertFalse(self.scheduler.reading_service.has_work())
        self.assertFalse(library.reading_path(PROJECT_ROOT/job.pages[0].txt).exists())

    def test_manual_request_only_enqueues_current_page_and_deduplicates(self):
        job = self.fixture(count=2)
        source = PROJECT_ROOT / job.pages[0].txt
        self.assertTrue(self.scheduler.request_reading(source))
        self.assertFalse(self.scheduler.request_reading(source))
        self.assertEqual(self.scheduler.reading_service.keys, {(job.jid, 1)})
        self.assertFalse(job.pages[1].reading_requested)
        self.run_draft(job)
        self.assertFalse(self.scheduler.request_reading(source))

    def test_removed_original_theme_is_not_listed_from_reading_copy(self):
        topics.create_topic('待删除', self.output)
        (self.output / library.ORIGINAL / '待删除').rmdir()
        self.assertTrue((self.output / library.READING / '待删除').exists())
        self.assertNotIn('待删除', library.topics())

    def test_deleted_original_is_not_downloaded_again_on_restart(self):
        job = self.fixture()
        (PROJECT_ROOT / job.pages[0].txt).unlink()
        restored = Task.from_dict(job.to_dict())
        self.assertEqual(restored.pages[0].status, '失败')
        self.assertEqual(restored.status, '失败')
        self.assertIn('外部删除', restored.pages[0].error)

    def test_save_original_does_not_enqueue_reading(self):
        job = self.fixture()
        self.scheduler._save(job, job.pages[0], [dict(start=0,end=1,text='原文100000元。')], '测试')
        self.assertEqual(job.pages[0].reading_status, '未整理')
        self.assertFalse(job.pages[0].reading_requested)
        self.assertFalse(self.scheduler.reading_service.has_work())

    def test_completed_historical_draft_is_not_automatically_rewritten(self):
        job=self.fixture();self.run_draft(job)
        with patch.object(reading,'history_enabled',return_value=False):
            self.scheduler.reading_service.scan()
        self.assertFalse(self.scheduler.reading_service.has_work())

    def test_unowned_original_is_not_automatically_rewritten_before_history_validation(self):
        job=self.fixture()
        self.scheduler.jobs=[]
        with patch.object(reading,'history_enabled',return_value=False):
            self.scheduler.reading_service.scan()
        self.assertFalse(self.scheduler.reading_service.has_work())

    def test_unverified_word_is_rejected_without_partial_sentence_splicing(self):
        source='我觉得原来的剧形可能匹配不了。比如说预算100000元。'
        candidate='我认为原来的剧情可能匹配不了。例如预算100000元。'
        self.assertTrue(written.guard(source,candidate))

    def test_paragraph_break_keeps_closing_quote_with_its_sentence(self):
        source='前面内容。'*20+'他说：“引用的内容。”随后继续说明。'
        formatted=reading.format_paragraphs(source)
        self.assertNotIn('。\n\n”',formatted)
        self.assertEqual(reading._letters(formatted),reading._letters(source))

    def test_edit_pipeline_rejects_unverified_word_without_splicing_sentences(self):
        original='我觉得原来的剧形可能匹配不了。比如说预算100000元。'
        seen=[]
        class Written(Editor):
            def generate(self,system,content,schema,cancel=None):
                result=super().generate(system,content,schema,cancel)
                if 'units' in result:
                    result['units'][0]['text']='我认为原来的剧情可能匹配不了。例如预算100000元。'
                else:
                    seen.append(json.loads(content))
                return result
        result=reading.organize_chunk([dict(id=0,start='00:00',text=original)],Written())
        self.assertIn('剧形',result[0]['text'])
        self.assertNotIn('剧情',result[0]['text'])
        self.assertEqual(result[0]['mode'],'原文排版')
        self.assertEqual(result[0]['text'],original)
        self.assertEqual(seen,[])

    def test_missing_information_points_and_boolean_id_cannot_pass(self):
        original=[dict(id=0,start='00:00',text='预算100000元，不是10000元。')]
        for change in ('points','id','audit_id'):
            class Invalid(Editor):
                def generate(self,*args,**kwargs):
                    result=super().generate(*args,**kwargs)
                    if 'units' in result:
                        if change=='points':result['units'][0]['information_points']=[]
                        elif change=='id':result['units'][0]['id']=False
                    elif change=='audit_id':result['checks'][0]['id']=False
                    return result
            result=reading.organize_chunk(original,Invalid())
            self.assertEqual(result[0]['text'],original[0]['text'])
            self.assertEqual(result[0]['mode'],'原文排版')

    def test_model_initialization_or_oom_keeps_completed_original_and_exports_layout(self):
        class OOM(Editor):
            def start(self,cancel=None):raise RuntimeError('CUDA out of memory')
        job=self.fixture()
        self.run_draft(job,OOM(),True)
        self.assertEqual(job.status,'完成')
        self.assertTrue((PROJECT_ROOT/job.pages[0].txt).exists())
        self.assertTrue((PROJECT_ROOT/job.pages[0].reading).exists())
        raw=textout.read_document(PROJECT_ROOT/job.pages[0].txt)['raw']
        self.assertEqual(raw['reading']['status'],'原文排版')
        self.assertIn('CUDA',raw['reading']['model_error'])

    def test_long_unpunctuated_import_is_bounded_without_losing_characters(self):
        source='你好，Hello world 100000元。'*200
        document=dict(text='# 长稿\n来源：url\n\n[00:00] '+source,url='url',raw=None)
        units=reading.source_units(document)
        self.assertTrue(all(len(u['text'])<=700 for u in units))
        self.assertEqual(''.join(u['text'] for u in units),source)

    def test_multipart_queue_save_failure_rolls_back_both_variants(self):
        job=self.fixture(count=2)
        for page in job.pages:
            self.scheduler.reading_service.enqueue((job.jid,page.number))
        with patch.object(reading,'enabled',return_value=False):
            while self.scheduler.reading_service.has_work():self.scheduler.reading_service.step(Editor())
        paths=[PROJECT_ROOT/p for page in job.pages for p in (page.txt,page.reading,page.raw)]
        before=[p.read_bytes() for p in paths]
        state=copy.deepcopy(job.to_dict())
        with patch('src.services.scheduler.atomic_json',side_effect=PermissionError('保存失败')):
            with self.assertRaises(PermissionError):self.scheduler.rename_transcript(job.jid,'新名')
        self.assertEqual(job.to_dict(),state)
        self.assertEqual([p.read_bytes() for p in paths],before)

    def test_resume_skips_completed_chunks(self):
        job=self.fixture(units=24)
        service=self.scheduler.reading_service
        service.enqueue((job.jid,1))
        editor=Editor()
        with patch.object(reading,'enabled',return_value=True),patch.object(reading_service,'resources_ready',return_value=True),patch.object(reading,'chunks',side_effect=lambda units: [units[:2]]):
            service.step(editor)
            initial=textout.read_document(PROJECT_ROOT/job.pages[0].txt)['raw']['reading']['blocks']
            self.assertEqual(len(initial),2)
            # A fresh service reads the saved prefix rather than redoing it.
            self.scheduler.reading_service=reading_service.ReadingService(self.scheduler)
            self.scheduler.reading_service.enqueue((job.jid,1))
            self.scheduler.reading_service.step(editor)
            restored=textout.read_document(PROJECT_ROOT/job.pages[0].txt)['raw']['reading']['blocks']
            self.assertEqual([u['id'] for u in restored],[0,1,2,3])
            self.assertEqual(restored[:2],initial)

    def test_existing_draft_regeneration_rename_updates_ownership_hash(self):
        job=self.fixture()
        self.run_draft(job,Editor(),True)
        source=PROJECT_ROOT/job.pages[0].txt
        source.write_text(source.read_text(encoding='utf-8').replace('100000','200000'),encoding='utf-8')
        self.run_draft(job,Editor(lambda:self.scheduler.rename_transcript(job.jid,'重新命名')),True)
        self.assertEqual(job.pages[0].reading_status,'完成')
        self.assertIn('200000',(PROJECT_ROOT/job.pages[0].reading).read_text(encoding='utf-8'))

    def test_unmanaged_reading_conflict_is_preserved(self):
        job=self.fixture()
        dest=library.reading_path(PROJECT_ROOT/job.pages[0].txt)
        dest.parent.mkdir(parents=True)
        dest.write_text('自己的阅读稿',encoding='utf-8')
        self.run_draft(job)
        self.assertEqual(dest.read_text(encoding='utf-8'),'自己的阅读稿')
        self.assertEqual(job.pages[0].reading_status,'整理失败')

    def test_false_semantic_review_returns_original_even_when_keywords_match(self):
        class Disagree(Editor):
            def generate(self,system,content,schema,cancel=None):
                result=super().generate(system,content,schema,cancel)
                if 'checks' in result:
                    for check in result['checks']:check.update(accepted=False,reason='观点含义不确定')
                return result
        original=[dict(id=0,start='00:00',text='我认为这个例子有意义。')]
        result=reading.organize_chunk(original,Disagree())
        self.assertEqual(result[0]['mode'],'原文排版')
        self.assertEqual(result[0]['text'],original[0]['text'])

    def test_corrupt_queue_does_not_block_or_move_original(self):
        legacy=self.output/'主题'/'旧稿.txt'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('# 旧稿\n[00:00] 正文',encoding='utf-8')
        self.state.write_text('{broken',encoding='utf-8')
        library_migration.migrate_library(self.state,root=self.output)
        self.assertTrue(legacy.exists())
        self.assertEqual(self.state.read_text(encoding='utf-8'),'{broken')

    def test_missing_model_does_not_downgrade_existing_valid_draft(self):
        job=self.fixture()
        self.run_draft(job,Editor(),True)
        before=(PROJECT_ROOT/job.pages[0].reading).read_bytes()
        self.run_draft(job,Editor(),False)
        self.assertEqual((PROJECT_ROOT/job.pages[0].reading).read_bytes(),before)
        self.assertEqual(job.pages[0].reading_status,'完成')

    def test_completed_cached_status_is_persisted_after_restart(self):
        job=self.fixture()
        self.run_draft(job,Editor(),True)
        job.pages[0].reading_status='待整理'
        self.scheduler._persist()
        self.run_draft(job,Editor(),True)
        stored=json.loads(self.state.read_text(encoding='utf-8'))
        self.assertEqual(stored['jobs'][0]['pages'][0]['reading_status'],'完成')

    def test_new_tasks_take_priority_over_historical_reading(self):
        first=self.fixture()
        service=self.scheduler.reading_service
        service.enqueue((first.jid,1),historical=True)
        self.assertFalse(service.has_work(allow_history=False))
        service.enqueue('new-task',historical=False)
        self.assertTrue(service.has_work(allow_history=False))
        self.assertEqual(service.pending.get_nowait()[2],'new-task')

    def test_empty_legacy_topic_migrates_to_both_categories(self):
        (self.output/'空主题').mkdir(parents=True)
        library_migration.migrate_library(self.state,root=self.output)
        self.assertFalse((self.output/'空主题').exists())
        for category in (library.ORIGINAL,library.READING):
            self.assertTrue((self.output/category/'空主题').is_dir())

    def test_plain_historical_txt_keeps_its_first_sentence(self):
        document=dict(text='第一句也需要完整保留。\n第二句预算100000元。',url='',raw=None)
        units=reading.source_units(document)
        self.assertIn('第一句也需要完整保留。',''.join(u['text'] for u in units))


if __name__=='__main__':unittest.main()
