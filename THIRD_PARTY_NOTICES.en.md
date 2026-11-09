[简体中文](THIRD_PARTY_NOTICES.md) | [English](THIRD_PARTY_NOTICES.en.md)

# Third-party notices

## QQ call audio components

The following files are derived from or adapt code from [`oldhan13/napcat-qq-voice-call-win`](https://github.com/oldhan13/napcat-qq-voice-call-win), reference commit `4f1bfeba20bcd7b8d335af03b54f38e743f1aa6d`:

- `alice/qq/call_pcm_worker.py`
- `alice/qq/native_call_control.mjs`

These files are marked `GPL-3.0-only`; the full license text is retained in [`alice/qq/COPYING.call-audio`](alice/qq/COPYING.call-audio). They are not relicensed under the root MIT license. Keep the upstream attribution and GPL terms with copies or modified versions of these files.

## Runtime dependencies

The repository does not vendor these packages. This inventory records the versions pinned in `deployment/cognition/requirements.lock` and `package-lock.json`; each link points to the exact published package version or its upstream license source. Preserve each package's own license and notices if you redistribute a bundled build.

| Package | Version | License | Source |
| --- | --- | --- | --- |
| annotated-types | 0.8.0 | MIT | [PyPI](https://pypi.org/project/annotated-types/0.8.0/) |
| anyio | 4.16.0 | MIT | [PyPI](https://pypi.org/project/anyio/4.16.0/) |
| asyncpg | 0.31.0 | Apache-2.0 | [PyPI](https://pypi.org/project/asyncpg/0.31.0/) |
| certifi | 2026.10.23 | MPL-2.0 | [PyPI](https://pypi.org/project/certifi/2026.10.23/) |
| distro | 1.9.0 | Apache-2.0 | [PyPI](https://pypi.org/project/distro/1.9.0/) |
| h11 | 0.16.0 | MIT | [PyPI](https://pypi.org/project/h11/0.16.0/) |
| httpcore | 1.0.9 | BSD-3-Clause | [PyPI](https://pypi.org/project/httpcore/1.0.9/) |
| httpx | 0.28.1 | BSD-3-Clause | [PyPI](https://pypi.org/project/httpx/0.28.1/) |
| idna | 3.20 | BSD-3-Clause | [PyPI](https://pypi.org/project/idna/3.20/) |
| jiter | 0.18.0 | MIT | [PyPI](https://pypi.org/project/jiter/0.18.0/) |
| openai | 2.36.0 | Apache-2.0 | [PyPI](https://pypi.org/project/openai/2.36.0/) |
| pydantic | 2.14.2 | MIT | [PyPI](https://pypi.org/project/pydantic/2.14.2/) |
| pydantic-core | 2.50.2 | MIT | [PyPI](https://pypi.org/project/pydantic-core/2.50.2/) |
| python-dotenv | 1.2.2 | BSD-3-Clause | [PyPI](https://pypi.org/project/python-dotenv/1.2.2/) |
| sniffio | 1.3.1 | MIT OR Apache-2.0 | [Upstream](https://github.com/python-trio/sniffio) |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | [PyPI](https://pypi.org/project/tqdm/4.70.1/) |
| typing-extensions | 4.16.0 | PSF-2.0 | [PyPI](https://pypi.org/project/typing-extensions/4.16.0/) |
| typing-inspection | 0.4.4 | MIT | [PyPI](https://pypi.org/project/typing-inspection/0.4.4/) |
| playwright-core | 1.63.0 | Apache-2.0 | [npm](https://www.npmjs.com/package/playwright-core/v/1.63.0) |
