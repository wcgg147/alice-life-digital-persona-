[简体中文](THIRD_PARTY_NOTICES.md) | [English](THIRD_PARTY_NOTICES.en.md)

# 第三方声明

## QQ 通话音频组件

以下文件源自或改编自 [`oldhan13/napcat-qq-voice-call-win`](https://github.com/oldhan13/napcat-qq-voice-call-win)，参考提交为 `4f1bfeba20bcd7b8d335af03b54f38e743f1aa6d`：

- `alice/qq/call_pcm_worker.py`
- `alice/qq/native_call_control.mjs`

这些文件标记为 `GPL-3.0-only`，完整许可证文本保存在 [`alice/qq/COPYING.call-audio`](alice/qq/COPYING.call-audio)。它们没有改用根目录 MIT 许可证。复制或修改这些文件时，请保留上游署名和 GPL 条款。

## 运行时依赖

仓库没有直接包含以下依赖包。本清单记录 `deployment/cognition/requirements.lock` 和 `package-lock.json` 中锁定的版本；链接指向对应发布版本或上游许可证来源。若重新分发包含这些依赖的打包版本，请保留各依赖自己的许可证和声明。

| 包 | 版本 | 许可证 | 来源 |
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
| sniffio | 1.3.1 | MIT OR Apache-2.0 | [上游](https://github.com/python-trio/sniffio) |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | [PyPI](https://pypi.org/project/tqdm/4.70.1/) |
| typing-extensions | 4.16.0 | PSF-2.0 | [PyPI](https://pypi.org/project/typing-extensions/4.16.0/) |
| typing-inspection | 0.4.4 | MIT | [PyPI](https://pypi.org/project/typing-inspection/0.4.4/) |
| playwright-core | 1.63.0 | Apache-2.0 | [npm](https://www.npmjs.com/package/playwright-core/v/1.63.0) |
