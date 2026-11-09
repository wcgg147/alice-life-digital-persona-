"""QQ's single native expression search and its GIF recommendation list."""
from __future__ import annotations

import base64
import hashlib
import io
import re
from urllib.parse import urlparse

from qq_tools import download_media

SEARCH_MEDIA_HOSTS = {'wa.qq.com'}


def image_details(data):
    from PIL import Image, ImageChops
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        if not (0 < width <= 2048 and 0 < height <= 2048 and width * height <= 4_000_000):
            raise ValueError('expression_dimensions_too_large')
        animated = False
        previous = image.convert('RGB')
        previous_duration = int(image.info.get('duration') or 0)
        # Merely having a second frame does not prove visible animation.
        for i in range(1, min(int(getattr(image, 'n_frames', 1)), 8)):
            image.seek(i)
            current = image.convert('RGB')
            bbox = ImageChops.difference(previous, current).getbbox()
            if bbox and 0 < previous_duration <= 2000:
                area = (bbox[2]-bbox[0]) * (bbox[3]-bbox[1])
                if area >= max(4, width * height * .01):
                    animated = True
            previous, previous_duration = current, int(image.info.get('duration') or 0)
        return {'width': width, 'height': height, 'animated': animated}


def _varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            break
        byte = data[offset]; offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError('invalid_native_protobuf_varint')


def _fields(data):
    offset, result = 0, {}
    count = 0
    while offset < len(data):
        count += 1
        if count > 10000:
            raise ValueError('too_many_native_fields')
        tag, offset = _varint(data, offset)
        field, wire = tag >> 3, tag & 7
        if not field:
            raise ValueError('invalid_native_protobuf_field')
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire in (1, 2, 5):
            length, offset = _varint(data, offset) if wire == 2 else (8 if wire == 1 else 4, offset)
            if offset + length > len(data):
                raise ValueError('truncated_native_protobuf')
            value, offset = data[offset:offset+length], offset+length
        else:
            raise ValueError('unsupported_native_protobuf_wire')
        result.setdefault(field, []).append(value)
    return result


def native_catalog(result, *, tab='all', recommendations=False):
    if result.get('source') != 'qq_native_panel' or not result.get('native_success') or not result.get('got_response') or result.get('native_error') != 0:
        raise ValueError('native_expression_search_failed')
    encoded = result.get('body_base64') or ''
    if len(encoded) > 2_800_000:
        raise ValueError('native_expression_response_too_large')
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('native_expression_response_too_large')
    root = _fields(raw)
    if recommendations:
        body = _fields(root.get(1, [b''])[0])
        if body.get(1, [0])[0] != 0:
            raise ValueError('native_gif_recommendations_failed')
        rows, url_field, md5_field = body.get(4, []), 11, 7
    else:
        if root.get(1) != [0x9038] or root.get(2) != [1] or root.get(3, [0])[0] != 0:
            raise ValueError('native_expression_envelope_failed')
        body = _fields(root.get(4, [b''])[0])
        if body.get(1, [0])[0] != 0:
            raise ValueError('native_expression_result_failed')
        rows, url_field, md5_field = body.get(7 if tab == 'composed' else 3, []), 10, 7
    catalog = []
    for raw_item in rows[:36]:
        item = _fields(raw_item)
        url = item.get(url_field, [b''])[0].decode('utf-8')
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in SEARCH_MEDIA_HOSTS or parsed.port not in (None,443) or parsed.username:
            continue
        digest = item.get(md5_field, [b''])[0].decode('ascii')
        catalog.append({'url':url, 'md5':digest, 'template_only':tab=='composed'})
    return catalog


async def search_stickers(query, *, api, animated_only=False, tab='all'):
    """Native query -> two originals; caller shows previews before choice."""
    query = str(query).strip()
    if tab not in {'all','gif','composed'} or (not query and tab != 'gif') or len(query)>24 or re.search(r'\d{5,}|https?://|@|[\r\n]',query):
        raise ValueError('use_short_public_expression_keywords')
    result = await api('qq_search_native_expressions', {'query':query, 'tab':tab})
    catalog = native_catalog(result, tab=tab, recommendations=tab=='gif' and not query)
    if tab == 'composed':
        # Native results contain backgrounds and layout/colors, not final text.
        raise ValueError('native_text_composition_not_connected')
    import httpx
    results = []
    attempts = 0
    for item in catalog:
        if (animated_only or tab=='gif') and not urlparse(item['url']).path.lower().endswith('.gif'):
            continue
        attempts += 1
        if attempts > 4:
            break
        try:
            data, mime = await download_media(item['url'], allowed_hosts=SEARCH_MEDIA_HOSTS)
            if re.fullmatch('[0-9a-fA-F]{32}', item['md5']) and hashlib.md5(data).hexdigest() != item['md5'].lower():
                continue
            details = image_details(data)
            if (animated_only or tab=='gif') and (mime != 'image/gif' or not details['animated']):
                continue
        except (ValueError,OSError,httpx.HTTPError):
            continue
        results.append({'id':hashlib.sha256(data).hexdigest()[:20], 'bytes':data, 'mime':mime, **details})
        if len(results)==2:
            break
    return results
