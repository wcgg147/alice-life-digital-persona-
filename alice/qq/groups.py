"""List, enable or disable joined groups without changing any QQ membership."""
import argparse
import asyncio
import json
from pathlib import Path
import websockets


async def main(args):
    path = Path(args.config)
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    async with websockets.connect(config['onebot_url'], additional_headers={
        'Authorization': 'Bearer ' + config['onebot_token']
    }) as ws:
        await ws.send(json.dumps({'action': 'get_group_list', 'params': {}, 'echo': 'groups'}))
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if event.get('echo') == 'groups':
                break
    if event.get('status') != 'ok':
        raise RuntimeError('group_list_failed')
    groups = event.get('data') or []
    allowed = {str(g) for g in config.get('allowed_groups', [])}
    if args.enable or args.disable:
        query = args.enable or args.disable
        hits = [g for g in groups if query in {str(g['group_id']), g['group_name']}]
        if args.disable and query.isdigit() and query in allowed:
            allowed.discard(query)
        elif len(hits) != 1:
            raise ValueError('Group is absent or name is ambiguous; specify its exact numeric ID')
        else:
            group = hits[0]
            if args.enable:
                allowed.add(str(group['group_id']))
            else:
                allowed.discard(str(group['group_id']))
        config['allowed_groups'] = sorted(allowed)
        config['proactive_groups'] = [str(g) for g in config.get('proactive_groups',[]) if str(g) in allowed]
        temporary = path.with_suffix('.new.json')
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    print(json.dumps([{'id': str(g['group_id']), 'name': g['group_name'],
                       'enabled': str(g['group_id']) in allowed} for g in groups], ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--enable')
    action.add_argument('--disable')
    asyncio.run(main(parser.parse_args()))
