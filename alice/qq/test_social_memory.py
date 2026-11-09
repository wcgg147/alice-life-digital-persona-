import json
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch

from channel import Store
from social_memory import SocialMemory


class SocialMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'qq.sqlite3')
        self.config={'enabled':True,'expected_qq':'1','allowed_groups':['111','222'],
                     'allowed_private_users':['2','3'],'social_memory_calls_per_day':1,'social_memory_gap_seconds':60}
        self.memory=SocialMemory(self.config,self.store)

    async def asyncTearDown(self):
        self.store.db.close();self.temp.cleanup()

    def event(self,mid,actor,text,*,scope='111',name='阿强'):
        return {'post_type':'message','message_type':'private' if scope.startswith('private:') else 'group',
                'self_id':1,'group_id':scope,'user_id':actor,'message_id':mid,'time':time.time(),
                'sender':{'nickname':name,'card':name},'message':[{'type':'text','data':{'text':text}}]}

    def work(self,scope='111',actor='2',text='记得我的工作吗'):
        return {'group':scope,'actor':actor,'text':text,'event':{'message_id':999}}

    def save(self,event,category,quote):
        scope=event['group_id'];self.memory.observe(event,scope=scope)
        rows=self.store.db.execute('SELECT id,message_id,actor,content FROM qq_observed_messages WHERE scope=? AND message_id=?',(scope,str(event['message_id']))).fetchall()
        return self.memory.apply_observations(scope,rows,{'observations':[{'source_id':str(event['message_id']),'category':category,'quote':quote}]})

    async def test_same_person_is_recognized_across_public_group_and_private_chat(self):
        self.save(self.event(11,2,'我现在做电焊工，平时也看比赛。'),'occupation','我现在做电焊工')
        self.memory.observe(self.event(12,2,'你好',scope='private:2',name='换个名字'))
        packet=self.memory.recall(self.work('private:2'))
        fact=packet['person']['quoted_self_reports'][0]
        self.assertEqual(fact['speaker_qq'],'2');self.assertEqual(fact['scope'],'111')
        self.assertEqual(fact['quotation'],'我现在做电焊工')

    async def test_private_evidence_never_enters_any_group_or_another_private_chat(self):
        self.save(self.event(11,2,'我在秘密项目里当分析师',scope='private:2'),'occupation','我在秘密项目里当分析师')
        for work in [self.work('111'),self.work('222'),self.work('private:3','3')]:
            self.assertNotIn('秘密项目',json.dumps(self.memory.recall(work),ensure_ascii=False))

    async def test_prior_private_contact_is_known_without_exposing_private_details(self):
        source=self.event(11,2,'秘密项目',scope='private:2')
        self.store.message('private-turn',source,'user','秘密项目',11,True)
        self.store.message('private-turn',source,'assistant','知道了',12,True)
        self.memory.seed_delivered_dialogues()
        packet=self.memory.recall(self.work(text='我之前跟你聊过吗'))
        self.assertTrue(packet['current_person']['has_prior_direct_interaction'])
        self.assertTrue(packet['current_person']['has_prior_private_interaction'])
        self.assertTrue(packet['private_details_withheld'])
        self.assertNotIn('秘密项目',json.dumps(packet,ensure_ascii=False))

    async def test_private_topic_presence_is_verified_without_putting_its_content_in_group(self):
        source=self.event(11,2,'我在看CS比赛，看的烧心',scope='private:2')
        self.store.message('private-turn',source,'user','我在看CS比赛，看的烧心',11,True)
        self.store.message('private-turn',source,'assistant','你在看哪队',12,True)
        self.memory.seed_delivered_dialogues()
        packet=self.memory.recall(self.work(text='我之前私聊告诉你的比赛是哪场'))
        self.assertTrue(packet['private_topic_seen'])
        self.assertNotIn('烧心',json.dumps(packet,ensure_ascii=False))

    async def test_person_facts_do_not_follow_other_people_with_same_nickname(self):
        self.save(self.event(11,2,'我是电焊工'),'occupation','我是电焊工')
        self.save(self.event(12,3,'我是咖啡师'),'occupation','我是咖啡师')
        two=self.memory.recall(self.work(actor='2'))['person']['quoted_self_reports']
        three=self.memory.recall(self.work(actor='3'))['person']['quoted_self_reports']
        self.assertEqual([f['quotation'] for f in two],['我是电焊工'])
        self.assertEqual([f['quotation'] for f in three],['我是咖啡师'])
        ambiguous=self.memory.recall(self.work(text='阿强的工作是什么'))
        self.assertEqual(len(ambiguous['ambiguous_named_people']),2)
        self.assertEqual(ambiguous['person']['quoted_self_reports'],[])

    async def test_named_member_recall_keeps_current_speaker_distinct(self):
        self.memory.observe(self.event(10,2,'你好',name='小李'))
        self.save(self.event(11,3,'我是咖啡师',name='阿强'),'occupation','我是咖啡师')
        packet=self.memory.recall(self.work(text='阿强的工作是什么'))
        self.assertEqual(packet['current_person']['qq_id'],'2')
        self.assertEqual(packet['person']['qq_id'],'3')

    async def test_group_question_can_recall_several_members_but_not_private_job(self):
        self.save(self.event(11,2,'我是电焊工',name='小李'),'occupation','我是电焊工')
        self.save(self.event(12,3,'我是咖啡师',name='阿强'),'occupation','我是咖啡师')
        self.save(self.event(13,3,'我在秘密项目当分析师',scope='private:3'),'occupation','我在秘密项目当分析师')
        packet=self.memory.recall(self.work(text='大家分别做什么工作'))
        self.assertEqual({r['speaker_qq'] for r in packet['group_members_quoted_self_reports']},{'2','3'})
        self.assertNotIn('秘密项目',json.dumps(packet,ensure_ascii=False))

    async def test_one_group_member_statement_is_not_presented_as_group_consensus(self):
        self.save(self.event(11,3,'我觉得绿龙比赛最好看'),'group_topic','我觉得绿龙比赛最好看')
        packet=self.memory.recall(self.work(text='你记得群里说过绿龙比赛吗'))
        note=packet['group_topic_statements'][0]
        self.assertEqual(note['speaker_qq'],'3')
        self.assertEqual(note['source_message_id'],'11')
        self.assertEqual(packet['shared_experiences'],[])

    async def test_older_person_fact_survives_outside_recent_message_window(self):
        self.save(self.event(11,2,'我是电焊工'),'occupation','我是电焊工')
        for i in range(220):self.memory.observe(self.event(100+i,2,'今天聊点别的'+str(i)))
        packet=self.memory.recall(self.work())
        self.assertEqual(packet['person']['quoted_self_reports'][0]['source_message_id'],'11')

    async def test_unproven_quotes_and_invented_message_ids_cannot_be_written(self):
        event=self.event(11,2,'我是电焊工');self.memory.observe(event)
        rows=self.store.db.execute('SELECT id,message_id,actor,content FROM qq_observed_messages').fetchall()
        saved=self.memory.apply_observations('111',rows,{'observations':[
            {'source_id':'11','category':'occupation','quote':'我是医生'},
            {'source_id':'999','category':'occupation','quote':'我是电焊工'}]})
        self.assertEqual(saved,0)

    async def test_alice_or_operator_messages_do_not_become_other_persons_self_report(self):
        self.memory.observe(self.event(11,1,'我是医生'))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM qq_observed_messages').fetchone()[0],0)
        self.store.sent_receipt('111','2','turn',12)
        self.memory.observe(self.event(12,1,'我今天有点累'))
        # 5 分钟前：要老过 120 秒的去抖，又要在 48 小时读窗口内
        self.store.db.execute('UPDATE qq_observed_messages SET observed_at=?',(time.time()-300,));self.store.db.commit()
        self.assertEqual(self.memory.pending_batch()[0],[])

    async def test_failed_or_unsent_reply_is_not_a_shared_experience(self):
        work=self.work();work.update(text='你好',event=self.event(11,2,'你好'))
        self.memory.remember_delivery(work,[])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM qq_dialogue_episodes').fetchone()[0],0)
        self.memory.remember_delivery(work,[(42,'真的说过的话')])
        packet=self.memory.recall(self.work(text='真的说过的话'))
        self.assertEqual(packet['shared_experiences'][0]['alice_actually_replied'],'真的说过的话')

    async def test_disabled_context_is_not_observed_or_recalled(self):
        self.memory.observe(self.event(11,2,'不应读到',scope='333'))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM qq_observed_messages').fetchone()[0],0)
        self.save(self.event(12,2,'我是电焊工'),'occupation','我是电焊工')
        self.config['allowed_groups']=[]
        self.assertEqual(self.memory.recall(self.work()),{})

    async def test_cold_extraction_is_bounded_and_uses_original_quote(self):
        self.memory.observe(self.event(11,2,'我是电焊工'))
        # 5 分钟前：要老过 120 秒的去抖，又要在 48 小时读窗口内
        self.store.db.execute('UPDATE qq_observed_messages SET observed_at=?',(time.time()-300,));self.store.db.commit()
        completion=AsyncMock(return_value=json.dumps({'observations':[{'source_id':'11','category':'occupation','quote':'我是电焊工'}]},ensure_ascii=False))
        await self.memory.consolidate(completion);await self.memory.consolidate(completion)
        completion.assert_awaited_once()
        self.assertEqual(self.memory.recall(self.work())['person']['quoted_self_reports'][0]['quotation'],'我是电焊工')

    async def test_permission_revoked_during_extraction_blocks_late_write(self):
        self.memory.observe(self.event(11,2,'我是电焊工'))
        # 5 分钟前：要老过 120 秒的去抖，又要在 48 小时读窗口内
        self.store.db.execute('UPDATE qq_observed_messages SET observed_at=?',(time.time()-300,));self.store.db.commit()
        async def completion(*args,**kwargs):
            self.config['allowed_groups']=[]
            return json.dumps({'observations':[{'source_id':'11','category':'occupation','quote':'我是电焊工'}]},ensure_ascii=False)
        await self.memory.consolidate(completion)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM qq_social_facts').fetchone()[0],0)

    async def test_failed_batch_is_retained_without_automatic_model_retries(self):
        self.config['social_memory_calls_per_day']=6
        self.memory.observe(self.event(11,2,'我是电焊工'))
        # 5 分钟前：要老过 120 秒的去抖，又要在 48 小时读窗口内
        self.store.db.execute('UPDATE qq_observed_messages SET observed_at=?',(time.time()-300,));self.store.db.commit()
        completion=AsyncMock(side_effect=RuntimeError('provider_failed'))
        await self.memory.consolidate(completion)
        self.store.db.execute('UPDATE qq_memory_runs SET at=0');self.store.db.commit()
        await self.memory.consolidate(completion)
        completion.assert_awaited_once()
        self.assertEqual(self.store.db.execute('SELECT processed FROM qq_observed_messages').fetchone()[0],-1)
        self.assertEqual(len(self.memory.pending_batch(immediate=True)[0]),1)

    async def test_read_window_excludes_stale_pending_but_keeps_failed_retry(self):
        self.memory.observe(self.event(11,2,'我是电焊工'))
        self.memory.observe(self.event(12,2,'今天加班到十点'))
        stale=time.time()-60*3600
        self.store.db.execute('UPDATE qq_observed_messages SET observed_at=?',(stale,));self.store.db.commit()
        self.assertEqual(self.memory.pending_batch(immediate=True)[0],[])
        self.assertEqual(self.memory.archive_stale(),2)
        # 失败待重试（-1）不受窗口限制，也不该被归档掉
        self.store.db.execute('UPDATE qq_observed_messages SET processed=-1,observed_at=?',(stale,));self.store.db.commit()
        self.assertEqual(self.memory.archive_stale(),0)
        self.assertEqual(len(self.memory.pending_batch(immediate=True)[0]),2)

    async def test_persona_batch_failure_does_not_stop_separate_qq_memory_path(self):
        from alice import Alice
        brain=Alice.__new__(Alice);brain.config=self.config;brain.store=self.store
        brain.consolidate=AsyncMock(side_effect=RuntimeError('growth_failed'))
        brain.completion=AsyncMock()
        brain.social_memory=type('Memory',(),{'consolidate':AsyncMock()})()
        with patch('alice.asyncio.sleep',AsyncMock(side_effect=[None,asyncio.CancelledError()])):
            with self.assertRaises(asyncio.CancelledError):await brain.cold_loop()
        brain.social_memory.consolidate.assert_awaited_once()

    async def test_noise_marked_fact_is_never_recalled(self):
        self.save(self.event(11,2,'我是电焊工'),'occupation','我是电焊工')
        self.assertEqual(len(self.memory.recall(self.work())['person']['quoted_self_reports']),1)
        self.store.db.execute("UPDATE qq_social_facts SET status='noise'");self.store.db.commit()
        self.assertEqual(self.memory.recall(self.work())['person']['quoted_self_reports'],[])

    async def test_insights_are_source_bound_and_never_taken_from_private_chat(self):
        self.memory.observe(self.event(11,2,'这个工具回包太慢了，建议加个超时'))
        rows=self.store.db.execute('SELECT id,message_id,actor,content FROM qq_observed_messages').fetchall()
        payload={'observations':[],'insights':[{'source_id':'11','kind':'defect','quote':'这个工具回包太慢了',
                 'why':'工具超时会拖住回复','action':'加超时'}]}
        self.memory.apply_observations('111',rows,payload)
        self.assertEqual(self.store.db.execute('SELECT scope,kind,quote,status FROM qq_project_insights').fetchall(),
                         [('111','defect','这个工具回包太慢了','observed')])
        self.memory.observe(self.event(12,2,'这个工具回包太慢了，建议加个超时',scope='private:2'))
        rows=self.store.db.execute("SELECT id,message_id,actor,content FROM qq_observed_messages WHERE scope='private:2'").fetchall()
        self.memory.apply_observations('private:2',rows,{'observations':[],'insights':[{'source_id':'12',
            'kind':'defect','quote':'这个工具回包太慢了','why':'x','action':'y'}]})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM qq_project_insights').fetchone()[0],1)

    async def test_insight_quote_must_be_verbatim_and_known_kind(self):
        self.memory.observe(self.event(11,2,'这个工具回包太慢了'))
        rows=self.store.db.execute('SELECT id,message_id,actor,content FROM qq_observed_messages').fetchall()
        self.memory.apply_observations('111',rows,{'observations':[],'insights':[
            {'source_id':'11','kind':'defect','quote':'我希望它快一点','why':'x','action':'y'},
            {'source_id':'11','kind':'gossip','quote':'这个工具回包太慢了','why':'x','action':'y'}]})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM qq_project_insights').fetchone()[0],0)

    async def test_profile_drops_invented_members_and_meta_talk(self):
        for i in range(6):self.memory.observe(self.event(10+i,2,'今天晚上打不打快速'))
        completion=AsyncMock(return_value=json.dumps({'summary':'一个开黑群，晚上人最多','profile_type':'开黑群',
            'topics':['组队开黑','吐槽回复像AI'],'regulars':[{'name':'阿强','note':'常来'},{'name':'没这个人','note':'瞎写'}],
            'active_hours':'晚上','impression':'挺热闹'},ensure_ascii=False))
        card=await self.memory.refresh_profile(completion,'111')
        self.assertEqual([r['name'] for r in card['regulars']],['阿强'])
        self.assertEqual(card['topics'],['组队开黑'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM qq_group_profiles').fetchone()[0],1)
