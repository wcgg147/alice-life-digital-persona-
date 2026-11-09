"""Normalize provider tool serialization only on a current model response."""
import json
import re
import uuid
import xml.etree.ElementTree as ET


MARKER = re.compile(r'</?[｜|]{1,2}DSML[｜|]{1,2}', re.I)
BLOCK = re.compile(r'<[｜|]{1,2}DSML[｜|]{1,2}\s+calls>.*?</[｜|]{1,2}DSML[｜|]{1,2}\s+calls>', re.S | re.I)
SIMPLE_MARKER = re.compile(r'</?(?:qq_[a-z][a-z0-9_]*|web_search)(?=[\s>/])',re.I)
SIMPLE_BLOCK = re.compile(r'<(?P<name>qq_[a-z][a-z0-9_]*|web_search)\s*>.*?</(?P=name)\s*>',re.S|re.I)


def contains_tool_protocol(text):
    return bool(MARKER.search(str(text or '')) or SIMPLE_MARKER.search(str(text or '')))


def simple_arguments(root,definition):
    schema=definition.get('parameters') or {};properties=schema.get('properties') or {}
    if root.attrib or len(root)>10 or (root.text or '').strip():raise ValueError('invalid_serialized_tool')
    args={}
    for child in root:
        if child.tag in args or child.attrib or len(child) or (child.tail or '').strip():raise ValueError('invalid_serialized_argument')
        if properties and child.tag not in properties:raise ValueError('unknown_serialized_argument')
        value=child.text or '';kind=properties.get(child.tag,{}).get('type','string')
        if kind=='boolean':
            if value.strip() not in {'true','false'}:raise ValueError('invalid_serialized_boolean')
            value=value.strip()=='true'
        elif kind in {'integer','number','array','object'}:value=json.loads(value)
        args[child.tag]=value
    if any(key not in args for key in schema.get('required',[])):raise ValueError('missing_serialized_argument')
    return args


def normalize_tool_message(message, definitions):
    text = str(message.get('content') or '')
    if not contains_tool_protocol(text):
        return message
    if message.get('tool_calls'):
        # Never execute the same call twice when the provider supplies both forms.
        plain = SIMPLE_BLOCK.sub('',BLOCK.sub('', text)).strip()
        if contains_tool_protocol(plain):
            raise ValueError('malformed_tool_protocol')
        return {**message, 'content': plain}
    allowed = {d['function']['name']:d['function'] for d in definitions or []}
    calls = []
    for match in SIMPLE_BLOCK.finditer(text):
        block=match.group();name=match['name']
        if name not in allowed or len(block)>8000 or '<!' in block:raise ValueError('unknown_serialized_tool')
        args=simple_arguments(ET.fromstring(block),allowed[name])
        calls.append({'id':'qq-'+uuid.uuid4().hex,'type':'function',
                      'function':{'name':name,'arguments':json.dumps(args,ensure_ascii=False)}})
        if len(calls)>3:raise ValueError('too_many_serialized_tools')
    for match in BLOCK.finditer(text):
        block = match.group()
        if len(block) > 8000 or '<!' in block:
            raise ValueError('invalid_tool_protocol')
        xml = re.sub(r'<(/?)[｜|]{1,2}DSML[｜|]{1,2}\s+', r'<\1dsml_', block)
        root = ET.fromstring(xml)
        for invoke in root:
            name = invoke.attrib.get('name')
            if invoke.tag != 'dsml_invoke' or name not in allowed or len(invoke) > 10:
                raise ValueError('unknown_serialized_tool')
            args = {}
            for param in invoke:
                key = param.attrib.get('name')
                if param.tag != 'dsml_parameter' or not key or key in args or len(param):
                    raise ValueError('invalid_serialized_argument')
                value = param.text or ''
                args[key] = value if param.attrib.get('string') == 'true' else json.loads(value)
            calls.append({'id': 'qq-' + uuid.uuid4().hex, 'type': 'function',
                          'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}})
            if len(calls) > 3:
                raise ValueError('too_many_serialized_tools')
    plain = SIMPLE_BLOCK.sub('',BLOCK.sub('', text)).strip()
    if not calls or contains_tool_protocol(plain):
        raise ValueError('malformed_tool_protocol')
    return {**message, 'content': plain, 'tool_calls': calls}
