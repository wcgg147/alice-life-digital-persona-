import json
import unittest
from unittest.mock import AsyncMock

from alice import Alice
from tool_protocol import normalize_tool_message, contains_tool_protocol


RAW = '''<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="qq_find_expression">
<｜｜DSML｜｜ parameter name="query" string="true">黑白雨窗前的企鹅表情</｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ parameter name="select_for_reply" string="false">true</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>'''
DEFS = [{'type':'function','function':{'name':'qq_find_expression'}}]


class ProtocolTests(unittest.TestCase):
    def test_reported_simple_xml_is_typed_and_removed_from_chat(self):
        from qq_tools import TOOLS
        text='行，那我闭嘴。\n\n<qq_find_expression>\n<query>闭嘴</query>\n<select_for_reply>true</select_for_reply>\n</qq_find_expression>'
        result=normalize_tool_message({'content':text},TOOLS)
        self.assertEqual(result['content'],'行，那我闭嘴。')
        self.assertEqual(json.loads(result['tool_calls'][0]['function']['arguments']),{'query':'闭嘴','select_for_reply':True})
        for bad in ['<qq_choose_expression>','<qq_shell><command>test</command></qq_shell>',
                    '<qq_find_expression><query>a</query><query>b</query></qq_find_expression>',
                    '<qq_find_expression><query>x</query><select_for_reply>yes</select_for_reply></qq_find_expression>']:
            with self.subTest(bad=bad),self.assertRaises(ValueError):normalize_tool_message({'content':bad},TOOLS)

    def test_simple_xml_is_rejected_by_sender_and_old_history_filter(self):
        from qq_reply import reply_parts
        self.assertTrue(contains_tool_protocol('<qq_choose_expression><expression_id>x</expression_id></qq_choose_expression>'))
        with self.assertRaises(RuntimeError):reply_parts('hi <qq_find_expression><query>x</query></qq_find_expression>')

    def test_reported_raw_protocol_becomes_a_typed_known_tool_call(self):
        message = normalize_tool_message({'content': RAW}, DEFS)
        self.assertEqual(message['content'], '')
        function = message['tool_calls'][0]['function']
        self.assertEqual(function['name'], 'qq_find_expression')
        self.assertEqual(json.loads(function['arguments']), {'query':'黑白雨窗前的企鹅表情','select_for_reply':True})

    def test_unknown_or_malformed_protocol_cannot_become_chat_text(self):
        for text in [RAW.replace('qq_find_expression','arbitrary_shell'), RAW[:50]]:
            with self.assertRaises(ValueError):
                normalize_tool_message({'content':text}, DEFS)

    def test_structured_and_text_forms_do_not_execute_twice(self):
        call = {'id':'real','function':{'name':'qq_find_expression','arguments':'{}'}}
        result = normalize_tool_message({'content':RAW,'tool_calls':[call]}, DEFS)
        self.assertEqual(result['tool_calls'],[call])
        self.assertFalse(contains_tool_protocol(result['content']))


class ProtocolLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_simple_xml_uses_actual_expression_tool_and_empty_final_is_valid(self):
        from qq_tools import TOOLS
        brain=Alice.__new__(Alice);state={'selected':None}
        async def execute(name,args,work,current):
            self.assertEqual(name,'qq_choose_expression')
            current['selected']={'label':'实际候选'}
            return {'status':'selected_for_final_reply'}
        brain.tools=type('Tools',(),{'permitted':lambda s,w:True,'definitions':lambda s:TOOLS,
                                    'execute':AsyncMock(side_effect=execute)})()
        brain.model_message=AsyncMock(side_effect=[{'content':'<qq_choose_expression><expression_id>saved:real</expression_id><text_placement>separate</text_placement></qq_choose_expression>'},{'content':''}])
        self.assertEqual(await brain.run_tools([],{'text':'尴不尴尬'},state),'')
        self.assertEqual(state['selected']['label'],'实际候选')

    async def test_serialized_model_call_executes_then_returns_normal_text(self):
        brain = Alice.__new__(Alice)
        brain.tools = type('Tools',(),{'permitted':lambda s,w:True,'definitions':lambda s:DEFS,
            'execute':AsyncMock(return_value={'status':'ok'})})()
        brain.model_message = AsyncMock(side_effect=[{'content':RAW},{'content':'给你。'}])
        result = await brain.run_tools([],{}, {})
        self.assertEqual(result,'给你。')
        self.assertEqual(brain.tools.execute.await_count,1)

    async def test_final_request_keeps_definitions_but_forbids_more_tools(self):
        brain = Alice.__new__(Alice)
        brain.tools = type('Tools',(),{'permitted':lambda s,w:True,'definitions':lambda s:DEFS,
            'execute':AsyncMock(return_value={'status':'ok'})})()
        brain.model_message = AsyncMock(side_effect=[{'content':RAW},{'content':RAW},{'content':'好了'}])
        self.assertEqual(await brain.run_tools([],{},{}),'好了')
        self.assertEqual(brain.model_message.await_args.kwargs,{'tools':DEFS,'tool_choice':'none'})
        self.assertEqual(brain.tools.execute.await_count,2)

    async def test_protocol_after_budget_is_never_returned_as_public_text(self):
        brain = Alice.__new__(Alice)
        brain.tools = type('Tools',(),{'permitted':lambda s,w:True,'definitions':lambda s:DEFS,
            'execute':AsyncMock(return_value={'status':'ok'})})()
        brain.model_message = AsyncMock(return_value={'content':RAW})
        self.assertEqual(await brain.run_tools([],{},{}),'')
        self.assertEqual(brain.tools.execute.await_count,2)

    async def test_description_is_not_mistaken_for_an_explicit_send_request(self):
        brain=Alice.__new__(Alice)
        state={'catalog':{'saved:real':{}},'selected':None}
        defs=[{'type':'function','function':{'name':'qq_choose_expression'}}]
        async def execute(name,args,work,current):
            current['selected']={'label':'real'}
            return {'status':'selected_for_final_reply'}
        brain.tools=type('Tools',(),{'permitted':lambda s,w:True,'definitions':lambda s:defs,'execute':AsyncMock(side_effect=execute)})()
        call={'id':'select','function':{'name':'qq_choose_expression','arguments':'{"expression_id":"saved:real"}'}}
        brain.model_message=AsyncMock(side_effect=[{'content':'这张图是个懵脸。'},{'tool_calls':[call]},{'content':'给你。'}])
        self.assertEqual(await brain.run_tools([],{'text':'发个表情包'},state),'给你。')
        self.assertIsNotNone(state['selected'])
        self.assertEqual(brain.model_message.await_count,3)
