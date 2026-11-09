from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from urllib.parse import urlparse
from channel import Store, OneBot


async def reload_groups(path, config, bot, store):
    immutable = ('onebot_url', 'onebot_token', 'expected_qq', 'persona_user_id', 'product_root', 'model', 'search_enabled','account_owner_qq','voice_node_path')
    original = {key: config.get(key) for key in immutable}
    while True:
        await asyncio.sleep(1)
        try:
            updated = config_from(path)
            if any(updated.get(key) != value for key, value in original.items()):
                raise ValueError('connection_or_identity_changed_restart_required')
            if any(config.get(key) != updated.get(key) for key in ('enabled', 'allowed_groups', 'allowed_private_users','proactive_groups')):
                bot.gate.active.clear()
                print('Group reply permissions reloaded', flush=True)
            for key,default in {'enabled':False,'allowed_groups':[], 'allowed_private_users':[], 'daily_call_cap':100,
                                'public_source_read_enabled':False,
                                'web_search_provider':'bing_rss',
                                'public_source_resolver':'system',
                                'sticker_search_enabled':False,'ambient_stickers_enabled':True,'ambient_sticker_views_per_day':3,
                                'ambient_sticker_favorites_per_day':2,'ambient_sticker_gap_seconds':1800,
                                'ambient_sticker_limits_per_group':False,
                                'proactive_groups':[],'proactive_decisions_per_day':12,'proactive_gap_seconds':600,
                                'social_memory_enabled':True,'social_memory_calls_per_day':6,'social_memory_gap_seconds':1800,'bot_qq_ids':[],
                                'voice_messages_enabled':False,'voice_calls_per_day':300,'queued_turn_ttl_seconds':180}.items():
                config[key] = updated.get(key,default)
        except Exception as exc:
            if config.get('enabled'):
                store.metric('invalid_config_disabled', {'error_type': type(exc).__name__})
            config['enabled'] = False
            bot.gate.active.clear()


def config_from(path):
    config = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    endpoint = urlparse(config['onebot_url'])
    if endpoint.scheme != 'ws' or endpoint.hostname not in {'127.0.0.1', 'localhost'}:
        raise ValueError('OneBot must be local ws://127.0.0.1 or localhost')
    if not config.get('onebot_token') or not str(config.get('expected_qq', '')).isdigit():
        raise ValueError('OneBot token and actual QQ account are required')
    if not 1 <= int(config.get('daily_call_cap', 100)) <= 1000:
        raise ValueError('daily_call_cap must be 1..1000')
    if not isinstance(config.get('enabled', False), bool):
        raise ValueError('enabled must be boolean')
    if not isinstance(config.get('search_enabled', False), bool):
        raise ValueError('search_enabled must be boolean')
    if not isinstance(config.get('public_source_read_enabled', False), bool):
        raise ValueError('public_source_read_enabled must be boolean')
    if config.get('web_search_provider','bing_rss') not in {'bing_rss','deepseek_search'}:
        raise ValueError('unsupported web_search_provider')
    if config.get('public_source_resolver','system') not in {'system','cloudflare'}:
        raise ValueError('unsupported public_source_resolver')
    if not isinstance(config.get('sticker_search_enabled', False), bool):
        raise ValueError('sticker_search_enabled must be boolean')
    if not isinstance(config.get('ambient_stickers_enabled',True),bool):
        raise ValueError('ambient_stickers_enabled must be boolean')
    if not isinstance(config.get('ambient_sticker_limits_per_group',False),bool):
        raise ValueError('ambient_sticker_limits_per_group must be boolean')
    if not isinstance(config.get('social_memory_enabled',True),bool):
        raise ValueError('social_memory_enabled must be boolean')
    if not isinstance(config.get('voice_messages_enabled',False),bool):
        raise ValueError('voice_messages_enabled must be boolean')
    if not 0<=int(config.get('voice_calls_per_day',300))<=300:
        raise ValueError('voice_calls_per_day must be 0..300')
    if config.get('voice_messages_enabled') and not Path(config.get('voice_node_path','')).is_file():
        raise ValueError('voice_node_path must identify the configured Node executable')
    if not 30<=int(config.get('queued_turn_ttl_seconds',180))<=600:
        raise ValueError('queued_turn_ttl_seconds must be 30..600')
    if not 0<=int(config.get('social_memory_calls_per_day',6))<=24 or not 60<=int(config.get('social_memory_gap_seconds',1800))<=86400:
        raise ValueError('social memory limits invalid')
    if not 0 <= int(config.get('ambient_sticker_favorites_per_day',2)) <= int(config.get('ambient_sticker_views_per_day',3)) <= 10:
        raise ValueError('ambient sticker daily limits invalid')
    if not 60 <= int(config.get('ambient_sticker_gap_seconds',1800)) <= 86400:
        raise ValueError('ambient_sticker_gap_seconds invalid')
    groups = config.get('allowed_groups', [])
    if not isinstance(groups, list) or not all(str(group).isdigit() for group in groups):
        raise ValueError('allowed_groups must be an array of numeric group IDs')
    bots = config.get('bot_qq_ids', [])
    if not isinstance(bots, list) or not all(str(bot).isdigit() for bot in bots):
        raise ValueError('bot_qq_ids must be an array of numeric QQ IDs')
    proactive=[str(g) for g in config.get('proactive_groups',[])]
    if (not all(str(group).isdigit() for group in proactive)
            or not {str(group) for group in proactive}.issubset({str(group) for group in groups})):
        raise ValueError('proactive_groups must be a subset of allowed_groups')
    config['proactive_groups']=proactive
    if not 0<=int(config.get('proactive_decisions_per_day',12))<=24 or not 300<=int(config.get('proactive_gap_seconds',600))<=86400:
        raise ValueError('proactive conversation limits invalid')
    users = config.get('allowed_private_users', [])
    if not isinstance(users, list) or not all(str(user).isdigit() for user in users):
        raise ValueError('allowed_private_users must be an array of numeric QQ IDs')
    if config.get('account_owner_qq') and str(config['account_owner_qq']) not in {str(u) for u in users}:
        raise ValueError('account owner must be an allowed private user')
    try:
        uuid.UUID(str(config.get('persona_user_id', '')))
    except (ValueError, AttributeError):
        raise ValueError('persona_user_id must be a UUID') from None
    return config


async def main(args):
    from alice import Alice
    config = config_from(args.config)
    lock = None
    if not args.check:
        import msvcrt
        lock = (Path(args.config).parent / 'channel.lock').open('a+b')
        lock.seek(0)
        if not lock.read(1):
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            lock.close()
            raise RuntimeError('QQ channel is already running')
    store = Store(Path(args.config).parent / 'channel.sqlite3')
    brain = Alice(config, store)
    while True:
        try:
            await brain.connect()
            break
        except (OSError,asyncio.TimeoutError) as exc:
            if args.check:
                raise
            print('Persona database unavailable; retry in 5s:',type(exc).__name__,flush=True)
            await asyncio.sleep(5)
    try:
        if args.check:
            facts = await brain.facts()
            print(json.dumps({'persona_facts': len(facts), 'model': config.get('model'),
                              'enabled': config.get('enabled', False),
                              'allowed_groups': config.get('allowed_groups', []),
                              'model_calls': 0}, ensure_ascii=False))
        elif args.consolidate:
            print(json.dumps(await brain.consolidate(), ensure_ascii=False))
        else:
            bot = OneBot(config, store, brain)
            tasks = [asyncio.create_task(bot.run()), asyncio.create_task(brain.cold_loop()),
                     asyncio.create_task(reload_groups(args.config, config, bot, store)),
                     asyncio.create_task(bot.cognition.run())]
            try:
                await tasks[0]
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await brain.close()
        store.db.close()
        if lock:
            lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check', action='store_true')
    group.add_argument('--consolidate', action='store_true')
    asyncio.run(main(parser.parse_args()))
