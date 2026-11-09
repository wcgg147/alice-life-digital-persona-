"""Install a narrow read-only action into the pinned local SnowLuma bundle.

QQ's own native transport stays responsible for authentication and requests.
No GUI automation, external expression provider, or arbitrary packet tool.
"""
import argparse
import hashlib
from pathlib import Path

PINNED_SHA256='3577ebae0000e05e4a7a7f8d6eb755d5a1a1ce789e0ac80b9cb25908ed35b905'
BEGIN='// ALICE_QQ_NATIVE_SEARCH_BEGIN'
END='// ALICE_QQ_NATIVE_SEARCH_END'

def install(directory):
    directory=Path(directory).resolve()
    target=directory/'index.mjs'
    backup=directory/'index.before-native-search.mjs'
    original=backup.read_bytes() if backup.exists() else target.read_bytes()
    if hashlib.sha256(original).hexdigest()!=PINNED_SHA256:
        raise ValueError('unsupported_native_connector_build')
    text=target.read_text(encoding='utf-8')
    snippet=Path(__file__).with_name('native_search_action.mjs').read_text(encoding='utf-8')
    if BEGIN in text:
        start=text.index(BEGIN); end=text.index(END,start)+len(END)
    elif 'name: "qq_native_search_probe"' in text:
        start=text.index(' defineAction({\n  name: "qq_native_search_probe"')
        end=text.index('\n\tdefineAction({',start)
    else:
        anchor='var actions = [\n'
        start=text.index(anchor)+len(anchor);end=start
    if 'name: "fetch_sys_faces"' not in text[end:end+600]:
        raise ValueError('native_action_anchor_changed')
    if not backup.exists():backup.write_bytes(original)
    target.write_text(text[:start]+snippet+text[end:],encoding='utf-8')
    print('Installed native QQ expression action; restart connector to load it.')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('directory')
    install(parser.parse_args().directory)
