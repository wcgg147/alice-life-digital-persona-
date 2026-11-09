import asyncio
import unittest
from cognition import test_workspace as fixtures
from cognition.decision import working_context
from cognition.exploration import normalize_result
from cognition.loop import CognitiveLoop
from cognition.state import Ledger


class SearchPlanTests(unittest.TestCase):
    setUp=fixtures.WorkspaceTests.setUp
    tearDown=fixtures.WorkspaceTests.tearDown
    read=fixtures.WorkspaceTests.read
    proposal=fixtures.WorkspaceTests.proposal

    def tick(self,raw):
        class Model:
            async def complete(_,messages):return raw
        class Env:
            cfg={}
            async def observe(_):
                from cognition.exploration import catalog
                return catalog(self.ledger),[]
            def exploration_capabilities(_):return {'can_search':True,'providers':['bing_rss','bilibili']}
            async def search(_,query,goal,**kwargs):
                return normalize_result({'status':'ok','results':[{'url':'https://example.org/a','title':'实际候选'}]},query,kwargs['provider'])
            async def read(_,resource):
                return {'status':'ok','source_ref':resource['source_ref'],'text':'实际读取了该检索返回的来源。'*20}
        return asyncio.run(CognitiveLoop(self.ledger,Model(),Env(),daily_cap=0).tick(opportunity_source='experiment'))

    def plan(self):
        source=self.read('g',0)
        raw=self.proposal('g',source,method='search')
        raw['orientation']['intentions'][0]['search_plan']={'provider':'bilibili','query':'原模型自选查询'}
        return source,raw

    def test_plan_parameters_survive_reopen_and_record_a_different_executed_provider(self):
        _,raw=self.plan();self.assertEqual(self.tick(raw)['status'],'oriented')
        self.ledger.close();self.ledger=Ledger(self.path)
        intention=self.ledger.view()['workspace']['intentions'][0]
        self.assertEqual(intention['search_plan'],raw['orientation']['intentions'][0]['search_plan'])
        self.tick({'action':'search','goal':{'id':'g','status':'active'},'resource_id':'','reason':'选择另一入口',
            'predict_success':True,'next_check_seconds':60,'search_provider':'bing_rss','search_query':'不同查询'})
        self.assertTrue(self.ledger.view()['workspace']['intentions'][0]['actual_override'])

    def test_unavailable_provider_is_rejected_without_expanding_permissions(self):
        _,raw=self.plan();raw['orientation']['intentions'][0]['search_plan']['provider']='unavailable'
        self.assertEqual(self.tick(raw)['status'],'decision_error')

    def test_plan_is_optional_and_rest_remains_a_valid_choice(self):
        source=self.read('g',0)
        self.assertEqual(self.tick(self.proposal('g',source,method='search'))['status'],'oriented')
        self.assertEqual(self.tick({'action':'rest','goal':None,'resource_id':'','inquiry':None,
            'predict_success':False,'reason':'保留问题，暂时休息','next_check_seconds':60})['status'],'rest')

    def test_withdrawing_evidence_removes_parameterized_intention(self):
        source,raw=self.plan();self.tick(raw);self.ledger.revoke(source)
        self.assertFalse(self.ledger.view()['workspace']['intentions'])

    def test_reading_changed_search_result_does_not_erase_the_override(self):
        from cognition.exploration import catalog
        _,raw=self.plan();self.tick(raw)
        self.tick({'action':'search','goal':{'id':'g','status':'active'},'resource_id':'','reason':'自主改变方法',
            'predict_success':True,'next_check_seconds':60,'search_provider':'bing_rss','search_query':'其他查询'})
        resource=catalog(self.ledger)[0]
        result=self.tick({'action':'read','goal':{'id':'g','status':'active'},'resource_id':resource['id'],
            'reason':'读取这次实际结果','predict_success':True,'next_check_seconds':60,
            'inquiry':{'kind':'fact','criterion':'原文给出阈值','expected_progress':.5}})
        self.assertEqual(result['status'],'observed',self.ledger.view()['episodes'][0]['result'])
        self.assertTrue(self.ledger.view()['workspace']['intentions'][0]['actual_override'])
