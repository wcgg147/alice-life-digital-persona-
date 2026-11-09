import json,tempfile,unittest
from pathlib import Path
from community import Community


class CommunityBoundaryTests(unittest.TestCase):
    def test_patch_paths_cannot_escape_qq_or_modify_native_call_installer(self):
        for path in ('../alice.py','C:/secret.py','.env','native_call_install.py','x.ps1','dir/file.py'):
            with self.assertRaises((ValueError,TypeError)):Community.safe_path(path)
        self.assertEqual(Community.safe_path('qq_reply.py').name,'qq_reply.py')

    def test_candidates_are_screening_only_not_product_verdicts(self):
        from feedback_signals import is_feedback_candidate
        for text in ('你讲话一直这么肉麻的吗，还自我感觉良好','你怎么又把表情包发成代码了',
                     '你这语音一直没有声音','能不能别每条消息都引用','你能不能帮我写一个程序'):
            self.assertTrue(is_feedback_candidate(text),text)
        for text in ('你好','今晚玩守望吗','哈哈哈哈','',None,'print("hello")'):
            self.assertFalse(is_feedback_candidate(text),text)

    def test_intake_is_scoped_deduplicated_and_does_not_write_source_db(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);cfg={'onebot_url':'ws://127.0.0.1:13001','onebot_token':'test','expected_qq':'9',
                'persona_user_id':'11111111-1111-4111-8111-111111111111','enabled':True,
                'allowed_groups':['1'],'allowed_private_users':['2'],'account_owner_qq':'2'}
            config=base/'config.json';config.write_text(json.dumps(cfg),encoding='utf-8')
            from channel import Store
            from social_memory import SocialMemory
            store=Store(base/'channel.sqlite3');memory=SocialMemory(cfg,store)
            import time
            for scope,actor,mid,mentioned in [('1','3','a',True),('1','4','b',False),('private:2','2','c',False)]:
                segments=[{'type':'text','data':{'text':'你怎么又把表情包发成代码了'}}]
                if mentioned:segments.append({'type':'at','data':{'qq':'9'}})
                memory.observe({'message_type':'private' if scope.startswith('private:') else 'group','group_id':scope,
                    'user_id':actor,'self_id':'9','message_id':mid,'time':time.time(),'message':segments},scope=scope)
            store.db.commit()
            # A busy ordinary group must not push an older private complaint out
            # of the bounded intake window before eligibility is evaluated.
            store.db.executemany('INSERT INTO qq_observed_messages(scope,message_id,actor,role,content,sent_at,observed_at,source_kind) VALUES (?,?,?,?,?,?,?,?)',
                [('1','noise-'+str(n),'4','person','普通群聊',time.time(),time.time(),'native_event') for n in range(1300)])
            store.db.commit();before=list(store.db.iterdump())
            q=Community(config)
            try:
                result=q.intake();self.assertEqual(len(result['captured']),2)
                self.assertEqual(q.intake()['captured'],[])
                self.assertEqual(list(store.db.iterdump()),before)
                self.assertEqual({r[0] for r in q.db.execute('SELECT status FROM feedback')},{'observed'})
            finally:q.close();store.db.close()


class CommunityExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.config=self.base/'config.json'
        self.cfg={'onebot_url':'ws://127.0.0.1:13001','onebot_token':'test','expected_qq':'9',
            'persona_user_id':'11111111-1111-4111-8111-111111111111','enabled':True,
            'allowed_groups':['1'],'allowed_private_users':['2'],'account_owner_qq':'2'}
        self.config.write_text(json.dumps(self.cfg),encoding='utf-8');self.q=Community(self.config);self.key='a'*20
        self.q.db.execute('INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (self.key,'1','3','m','real complaint',1,1,1,'observed','{}',None));self.q.db.commit()
        from unittest.mock import AsyncMock
        self.q.flash=AsyncMock(return_value='{"edits":[]}')

    async def asyncTearDown(self):self.q.close();self.tmp.cleanup()

    def plan(self):return self.q.decide(self.key,{'status':'planned','reason':'fixed repro','allowed_paths':['dialogue_context.py'],
        'change_request':'small bounded edit','acceptance':['must pass original case']})

    async def test_unplanned_or_revoked_task_cannot_reach_model(self):
        with self.assertRaises(ValueError):await self.q.implement(self.key)
        self.plan();self.cfg['allowed_groups']=[];self.config.write_text(json.dumps(self.cfg),encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError,'revoked'):await self.q.implement(self.key)
        self.q.flash.assert_not_awaited()

    async def test_stale_baseline_is_rejected_before_model_call(self):
        self.plan();row=self.q.task(self.key);doc=json.loads(row['decision']);doc['baseline']['dialogue_context.py']='stale'
        self.q.db.execute('UPDATE feedback SET decision=? WHERE id=?',(json.dumps(doc),self.key));self.q.db.commit()
        with self.assertRaisesRegex(RuntimeError,'baseline_changed'):await self.q.implement(self.key)
        self.q.flash.assert_not_awaited()

    async def test_outside_patch_and_exhausted_attempts_cannot_apply_to_source(self):
        self.plan();self.q.flash.return_value='{"edits":[{"path":"../escape.py","before":"","after":"pass"}]}'
        with self.assertRaises(ValueError):await self.q.implement(self.key)
        self.q.db.executemany('INSERT INTO attempts VALUES (?,?,?,?,?)',[(1,'implement',self.key,'failed','')]*2);self.q.db.commit()
        self.q.flash.reset_mock()
        with self.assertRaisesRegex(RuntimeError,'two_implementation'):await self.q.implement(self.key)
        self.q.flash.assert_not_awaited()

    def test_verified_requires_ordered_states_and_independent_artifacts(self):
        with self.assertRaises(ValueError):self.q.decide(self.key,{'status':'verified','reason':'looks good'})
        self.q.db.execute("UPDATE feedback SET status='checking' WHERE id=?",(self.key,));self.q.db.commit()
        with self.assertRaisesRegex(ValueError,'artifact'):self.q.decide(self.key,{'status':'ready_for_acceptance','reason':'looks good'})


class BrowseGuardsTests(unittest.IsolatedAsyncioTestCase):
    def test_previously_enabled_public_actions_are_disabled_at_the_write_boundary(self):
        from types import SimpleNamespace
        from unittest.mock import Mock,patch
        from community_browse import readonly_platforms,disable_public_actions,BrowseControlError
        import os
        config=SimpleNamespace(ALICE_SPEAK_ENABLED=True)
        action=SimpleNamespace(enabled=lambda:True,apply=Mock())
        speak=SimpleNamespace(enabled=lambda:True,post_comment=Mock())
        x=SimpleNamespace(interact_enabled=lambda:True,speak_enabled=lambda:True);novel=SimpleNamespace(interact_enabled=lambda:True)
        with patch.dict(os.environ,{'ALICE_SPEAK_ENABLED':'true'}):
            readonly_platforms(config);disable_public_actions(action,speak,x,novel)
            self.assertEqual(os.environ['ALICE_SPEAK_ENABLED'],'false');self.assertFalse(config.ALICE_SPEAK_ENABLED)
            self.assertFalse(action.enabled());self.assertFalse(speak.enabled());self.assertFalse(x.speak_enabled());self.assertFalse(novel.interact_enabled())
            with self.assertRaises(BrowseControlError):speak.post_comment(1,'must not send')
            with self.assertRaises(BrowseControlError):action.apply('BVx',{})

    async def test_revoked_memory_write_cannot_be_swallowed_as_a_personality_choice(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from community_browse import GuardedMemory,BrowseControlError
        def revoked():raise RuntimeError('disabled')
        for name in ('append_life_item','set_persona_fact_value','record_life_action','upsert_life_item'):
            operation=AsyncMock();memory=SimpleNamespace(**{name:operation});guarded=GuardedMemory(memory,revoked)
            async def legacy_reader():
                try:await getattr(guarded,name)('persona',{})
                except Exception:return 'empty choice fallback'
            with self.assertRaises(BrowseControlError):await legacy_reader()
            operation.assert_not_awaited()
