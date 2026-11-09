import copy
import unittest

from cognition.decision import working_context,validate


class AttentionRetirementTests(unittest.TestCase):
    def state(self):
        return {'revision':1,'goals':[{'id':g,'question':g+'的问题是什么？','need':'text',
            'origin':'source:'+g,'status':status} for g,status in [('old','completed'),('now','active'),('later','paused')]],
            'episodes':[],'strategies':[],
            'motivation':{'goals':[{'goal_id':g,'opportunity_estimate':.25} for g in ['old','now','later']]},
            'question_states':{g:{'parts':[],'last_integration':g} for g in ['old','now','later']},
            'progress_memory':{g:[{'answer':g+'带来源的旧回答'}] for g in ['old','now','later']},
            'consolidations':[{'id':'memory','topic':'book:x','summary':'以前完成过一个阅读问题','open_question':'',
                               'episode_count':3,'start_ref':'book:x:0','latest_ref':'book:x:2','updated':1}]}
    def test_completed_detail_retires_without_mutating_history_or_forcing_action(self):
        s=self.state();before=copy.deepcopy(s);c=working_context(s,[],[])
        self.assertEqual(set(c['question_states']),{'now','later'})
        self.assertEqual(set(c['progress_memory']),{'now','later'})
        self.assertEqual({p['goal_id'] for p in c['motivation']['goals']},{'now','later'})
        self.assertEqual({g['id']:g for g in c['goals']},{g['id']:g for g in s['goals']})
        self.assertEqual(c['consolidations'],s['consolidations'])
        self.assertEqual(s,before)
        d={'action':'rest','resource_id':'','goal':None,'predict_success':False,'reason':'当前暂不行动','next_check_seconds':600}
        self.assertEqual(validate(d,c)['action'],'rest')
    def test_reactivation_restores_surviving_detailed_progress(self):
        s=self.state();s['goals'][0]['status']='active';c=working_context(s,[],[])
        self.assertEqual(c['progress_memory']['old'],s['progress_memory']['old'])
        self.assertEqual(c['question_states']['old'],s['question_states']['old'])
        self.assertIn('old',{p['goal_id'] for p in c['motivation']['goals']})
    def test_ablation_still_hides_derived_details(self):
        c=working_context(self.state(),[],[],False)
        self.assertFalse(c['progress_memory']);self.assertFalse(c['question_states']);self.assertFalse(c['motivation'])
