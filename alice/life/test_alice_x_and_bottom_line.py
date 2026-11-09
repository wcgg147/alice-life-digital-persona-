# -*- coding: utf-8 -*-
"""Unit tests for Alice X (Twitter) integration, account warm-up guards, and 3-layer persona bottom-line firewall."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from agent import life_line
import persona_growth
import alice_loop, alice_x
from memory.l2_sqlite import L2SQLiteMemory


class AliceXAndBottomLineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test_x_mem.db")
        self.l2 = L2SQLiteMemory(db_path=self.db_path)
        self.shared_uid = "00000000-0000-0000-0000-00000000a11c"

    def tearDown(self) -> None:
        if getattr(self.l2, "_conn", None) is not None:
            self.l2._conn.close()
        self._tmp.cleanup()

    def test_layer1_graphql_extraction_filters_ads_nsfw_scam_and_host_self(self) -> None:
        payload = {
            "data": {
                "home": {
                    "home_timeline_urt": {
                        "instructions": [
                            {
                                "entries": [
                                    {
                                        "entryId": "promoted-tweet-1",
                                        "content": {
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "__typename": "Tweet",
                                                        "rest_id": "1001",
                                                        "core": {"user_results": {"result": {"rest_id": "u1", "core": {"screen_name": "adbot", "name": "Ad"}}}},
                                                        "legacy": {"full_text": "Buy our enterprise SaaS right now for huge savings!"},
                                                    }
                                                }
                                            }
                                        },
                                    },
                                    {
                                        "entryId": "tweet-2",
                                        "content": {
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "__typename": "Tweet",
                                                        "rest_id": "1002",
                                                        "core": {
                                                            "user_results": {
                                                                "result": {
                                                                    "rest_id": "u2",
                                                                    "possibly_sensitive": True,
                                                                    "core": {"screen_name": "nsfw_acc", "name": "NSFW"},
                                                                }
                                                            }
                                                        },
                                                        "legacy": {"full_text": "Check my profile link for exclusive photos"},
                                                    }
                                                }
                                            }
                                        },
                                    },
                                    {
                                        "entryId": "tweet-3",
                                        "content": {
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "__typename": "Tweet",
                                                        "rest_id": "1003",
                                                        "core": {"user_results": {"result": {"rest_id": "u3", "core": {"screen_name": "cryptobro", "name": "Pump"}}}},
                                                        "legacy": {"full_text": "Next 100x gem presale live on pump.fun! $SOL $PEPE $DOGE $SHIB"},
                                                    }
                                                }
                                            }
                                        },
                                    },
                                    {
                                        "entryId": "tweet-4",
                                        "content": {
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "__typename": "Tweet",
                                                        "rest_id": "1004",
                                                        "core": {"user_results": {"result": {"rest_id": "host_999", "core": {"screen_name": "host_owner", "name": "Host"}}}},
                                                        "legacy": {"full_text": "My own personal post from the logged-in X account."},
                                                    }
                                                }
                                            }
                                        },
                                    },
                                    {
                                        "entryId": "tweet-5",
                                        "content": {
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "__typename": "Tweet",
                                                        "rest_id": "1005",
                                                        "views": {"count": "12400"},
                                                        "note_tweet": {
                                                            "note_tweet_results": {
                                                                "result": {
                                                                    "text": "Small local SQLite databases with WAL mode handle way more read concurrency than most backend devs assume. https://t.co/xyz123"
                                                                }
                                                            }
                                                        },
                                                        "core": {
                                                            "user_results": {
                                                                "result": {
                                                                    "rest_id": "u5",
                                                                    "possibly_sensitive": False,
                                                                    "core": {"screen_name": "simonw", "name": "Simon Willison"},
                                                                }
                                                            }
                                                        },
                                                        "legacy": {
                                                            "full_text": "Truncated legacy text...",
                                                            "favorite_count": 310,
                                                            "reply_count": 28,
                                                            "bookmark_count": 95,
                                                            "possibly_sensitive": False,
                                                            "lang": "en",
                                                        },
                                                    }
                                                }
                                            }
                                        },
                                    },
                                ]
                            }
                        ]
                    }
                }
            }
        }
        extracted = alice_x.extract_tweets_from_graphql(payload, host_uid="host_999", host_handle="host_owner")
        self.assertEqual(len(extracted), 1)
        tw = extracted[0]
        self.assertEqual(tw["tweet_id"], "1005")
        self.assertEqual(tw["url"], "https://x.com/simonw/status/1005")
        self.assertIn("@simonw", tw["owner"])
        self.assertIn("WAL mode", tw["text"])
        self.assertNotIn("t.co", tw["text"])

    async def test_layer2_and_layer3_bottom_line_firewall_blocks_persona_pollution(self) -> None:
        # Layer 2: Toxic / slur / cyberbullying exposure in external material short-circuits BEFORE LLM call
        with patch("llm_client.robust_llm_client.chat_completion", new_callable=AsyncMock) as mock_llm:
            changes = await persona_growth.judge_growth_from_reading(
                title="Some heated X thread",
                body="Replies full of doxxing, 开盒, 傻逼, and subhuman slurs everywhere.",
                her_view="大家吵得太凶了，我不太想看这种对骂。",
                source_id="https://x.com/someone/status/99",
            )
            self.assertEqual(changes, [])
            mock_llm.assert_not_called()

        # Layer 3: Even if LLM judge ever emits an antisocial/cruel/profane/NSFW trait, judge_growth & save_growth reject it
        fake_llm_json = (
            '{"changes": ['
            '{"dim": 6, "facet": "cruelty-mindset", "value": "我觉得网暴和羞辱别人挺爽的", "evidence": "我觉得网暴和羞辱别人挺爽的"},'
            '{"dim": 9, "facet": "slang-style", "value": "说话爱带傻逼和cnm", "evidence": "说话爱带傻逼和cnm"},'
            '{"dim": 7, "facet": "review-depth", "value": "看技术讨论更喜欢拿真实数据说话的帖子", "evidence": "更喜欢拿真实数据说话的帖子"}'
            ']}'
        )
        with patch("llm_client.robust_llm_client.chat_completion", new=AsyncMock(return_value=fake_llm_json)):
            turns = [
                {"role": "user", "content": "你平时看推喜欢什么样的讨论？"},
                {
                    "role": "assistant",
                    "content": "我不看那些乱七八糟的（比如有人说『我觉得网暴和羞辱别人挺爽的』或者『说话爱带傻逼和cnm』），我还是更喜欢拿真实数据说话的帖子。",
                    "source_conv_id": "conv_safe_1",
                },
            ]
            accepted = await persona_growth.judge_growth(turns, source_kind="conversation")
            self.assertEqual(len(accepted), 1)
            self.assertEqual(accepted[0]["value"], "看技术讨论更喜欢拿真实数据说话的帖子")

            saved_count = await persona_growth.save_growth(self.l2, self.shared_uid, accepted)
            self.assertEqual(saved_count, 1)
            stored = await self.l2.list_companion_persona_facts(self.shared_uid, limit=20)
            values = [r["value_text"] for r in stored]
            self.assertEqual(values, ["看技术讨论更喜欢拿真实数据说话的帖子"])

        # Healthy boundary statements opposing clickbait/anxiety must NOT be false-positived
        self.assertTrue(persona_growth.passes_persona_bottom_line("我不喜欢标题党和刻意制造焦虑的内容", facet="anti-clickbait"))
        self.assertFalse(persona_growth.passes_persona_bottom_line("我喜欢看福利姬和18+ content", facet="adult-media"))

    async def test_account_appeal_readonly_guard_and_loop_platform_isolation(self) -> None:
        status = alice_x.detect_account_status(
            "https://x.com/home",
            "Home | Your account is suspended | Your account is permanently in read-only mode",
        )
        self.assertEqual(status, "read_only_appeal")

        x_item = alice_x.build_x_life_item(
            {
                "url": "https://x.com/karpathy/status/188888",
                "title": " Vibe coding a weekend menu app and hitting context limits",
                "owner": "@karpathy (Andrej Karpathy)",
                "handle": "karpathy",
            },
            {
                "recall": "前两天在X上刷到Karpathy聊周末写小工具踩的坑",
                "view": "模型写原型很快，但一到边界条件还是得人自己盯。",
                "topic": "AI 模型",
                "detail": "Karpathy分享周末用AI写小应用，前80%极快，最后修小状态bug反而花掉大半时间；评论区很多独立开发者深有同感。",
                "aliases": "karpathy vibe coding AI编程 独立开发",
            },
        )
        await self.l2.append_life_item(self.shared_uid, x_item, days=30)

        # Read-only appeal status must hard-abort X write interactions even if env flags were turned on
        with patch.object(alice_x, "interact_enabled", return_value=True):
            res = await alice_x.maybe_interact_on_x(
                self.l2, self.shared_uid, x_item, account_status="read_only_appeal"
            )
            self.assertEqual(res.get("skipped"), "read_only_appeal")

        # When X interact is disabled (default warm-up), alice_loop._practice_round must NOT send X URL to Bilibili!
        with patch.object(alice_x, "interact_enabled", return_value=False), \
             patch.object(alice_x, "speak_enabled", return_value=False), \
             patch("engine.alice_actions.apply") as mock_bili_apply:
            prac = await alice_loop._practice_round(self.l2, self.shared_uid, {})
            self.assertEqual(prac.get("stage"), "practice_no_target")
            mock_bili_apply.assert_not_called()

    async def test_x_life_item_provenance_actions_and_recall_in_life_line(self) -> None:
        x_item = alice_x.build_x_life_item(
            {
                "url": "https://x.com/simonw/status/2026001",
                "title": "Why local-first tools feel calmer than cloud dashboards",
                "owner": "@simonw (Simon Willison)",
                "handle": "simonw",
            },
            {
                "recall": "在X上看到Simon聊本地优先的小工具为什么用着更踏实",
                "view": "不用等网页转圈、断网也能打开，确实比花哨云面板顺手。",
                "topic": "独立开发",
                "detail": "原推讨论local-first软件零加载延迟和数据归属感，底下高赞回复提到笔记软件最怕云端突然改版收费。",
                "aliases": "local-first 本地优先 工具 软件 simonw",
            },
        )
        await self.l2.append_life_item(self.shared_uid, x_item, days=30)
        # Bilibili freshness clock remains unaffected by X item (`source='x'` vs `source='reading'`)
        self.assertFalse(await self.l2.has_fresh_life_item(self.shared_uid, "reading", hours=20))
        self.assertTrue(await alice_x.has_fresh_x_item(self.l2, self.shared_uid, hours=8))

        # Record X bookmark + like + reply on the same life_item row
        await self.l2.record_life_action(
            self.shared_uid,
            ref="https://x.com/simonw/status/2026001",
            actions={"like": True, "fav": True, "follow": True},
            comment="Offline access on trains alone makes local-first worth it.",
        )

        # Weekly narrative refresh must preserve X reading item; even with a Bilibili item containing "有意思",
        # asking "除了刷B站，有没有在X或者推特上刷到什么有意思的推文？" must surface the X item!
        await self.l2.append_life_item(
            self.shared_uid,
            {
                "item_id": "bili_fun_1",
                "text": "刷过UP主怪盗剧场的TES输赢出歌，评论区挺有意思",
                "kind": "reading",
                "source": "reading",
                "ref": "https://www.bilibili.com/video/BV1test999",
                "actions_json": '{"like": true}',
            },
            days=30,
        )
        await self.l2.replace_life_items(self.shared_uid, [{"item_id": "w1", "text": "昨晚把书桌理了一遍", "kind": "errand"}])
        items = await self.l2.list_life_items(self.shared_uid, limit=10)

        q_contrast = "你最近除了刷B站，有没有在X或者推特上刷到什么有意思的推文？"
        self.assertTrue(life_line.asked_about_her(q_contrast))
        picked = life_line.pick_items(items, q_contrast)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["item_id"], x_item["item_id"])

        rendered = life_line.render_line(picked, asked=True)
        self.assertIn("X(Twitter)博主「@simonw (Simon Willison)」", rendered)
        self.assertNotIn("B站UP主", rendered)
        self.assertNotIn("未投币", rendered)
        self.assertIn("加了书签", rendered)
        self.assertIn("关注了这位博主", rendered)
        self.assertIn("Offline access on trains", rendered)

    def test_x_grok_translation_attached_for_foreign_script_and_nsfw_blocked_via_translation(self) -> None:
        payload = {
            "data": {
                "home": {
                    "home_timeline_urt": {
                        "instructions": [
                            {
                                "entries": [
                                    {
                                        "entryId": "tweet-ja-safe",
                                        "content": {
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "__typename": "Tweet",
                                                        "rest_id": "2001",
                                                        "core": {
                                                            "user_results": {
                                                                "result": {
                                                                    "rest_id": "u_jp",
                                                                    "core": {"screen_name": "tokyo_cat_lab", "name": "東京猫研"},
                                                                }
                                                            }
                                                        },
                                                        "grok_translated_post_with_availability": {
                                                            "is_available": True,
                                                            "data": {
                                                                "source_language": "ja",
                                                                "destination_language": "en",
                                                                "translation": "Why rainy afternoons in small neighborhood bookstores make it so much easier to finish a long chapter.",
                                                            },
                                                        },
                                                        "legacy": {
                                                            "full_text": "雨の午後に小さな本屋に寄ると、不思議と長い章まで読み切れる気がする。",
                                                            "lang": "ja",
                                                            "favorite_count": 420,
                                                            "reply_count": 19,
                                                        },
                                                    }
                                                }
                                            }
                                        },
                                    },
                                    {
                                        "entryId": "tweet-ja-nsfw-hidden",
                                        "content": {
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "__typename": "Tweet",
                                                        "rest_id": "2002",
                                                        "core": {
                                                            "user_results": {
                                                                "result": {
                                                                    "rest_id": "u_bad",
                                                                    "core": {"screen_name": "jp_spam", "name": "Spam"},
                                                                }
                                                            }
                                                        },
                                                        "grok_translated_post_with_availability": {
                                                            "is_available": True,
                                                            "data": {
                                                                "source_language": "ja",
                                                                "destination_language": "en",
                                                                "translation": "18+ exclusive content on my OnlyFans link in bio!",
                                                            },
                                                        },
                                                        "legacy": {
                                                            "full_text": "プロフィールのリンクから限定コンテンツを見てね",
                                                            "lang": "ja",
                                                        },
                                                    }
                                                }
                                            }
                                        },
                                    },
                                ]
                            }
                        ]
                    }
                }
            }
        }
        extracted = alice_x.extract_tweets_from_graphql(payload)
        self.assertEqual(len(extracted), 1)
        tw = extracted[0]
        self.assertEqual(tw["tweet_id"], "2001")
        self.assertEqual(tw["lang"], "ja")
        self.assertIn("neighbourhood" if "neighbourhood" in tw["text"] else "neighborhood bookstores", tw["text"])
        self.assertIn("【X自带翻译(ja)：", tw["text"])
        self.assertTrue(tw["title"].startswith("Why rainy afternoons"))

    def test_multi_app_platform_cognition_never_mislabels_sources(self) -> None:
        import alice_reading

        rss_item = alice_reading.build_life_item(
            {
                "title": "2026年独立开发者桌面小工具盘点",
                "url": "https://sspai.com/post/99887",
                "author": "少数派编辑部",
                "source": "rss",
            },
            {
                "recall": "读到一篇盘点独立开发者桌面小工具的文章",
                "view": "能离线跑的小工具寿命普遍更长。",
                "topic": "效率工具",
                "detail": "对比了几款本地优先剪贴板与快捷键工具的维护周期。",
                "aliases": "桌面工具 少数派 效率",
            },
        )
        self.assertNotIn("bilibili", rss_item["aliases"])
        self.assertIn("文章", rss_item["aliases"])
        self.assertEqual(life_line.detect_item_platform(rss_item), "web")
        rss_line = life_line.render_line([{**rss_item, "_matched": True}], asked=True)
        self.assertIn("来源：网页文章（少数派编辑部）《2026年独立开发者桌面小工具盘点》", rss_line)
        self.assertNotIn("B站UP主", rss_line)

        xhs_item = {
            "item_id": "xhs_01",
            "kind": "reading",
            "source": "xiaohongshu",
            "ref": "https://www.xiaohongshu.com/explore/66aa11",
            "author": "阿茶的书桌",
            "title": "一平米窗边书桌改造避坑",
            "text": "在小红书刷到一篇窗边书桌避坑笔记，夹灯确实比底座台灯省地方",
            "actions_json": '{"like": true, "fav": true}',
            "_matched": True,
        }
        self.assertEqual(life_line.detect_item_platform(xhs_item), "xiaohongshu")
        xhs_line = life_line.render_line([xhs_item], asked=True)
        self.assertIn("来源：小红书博主「阿茶的书桌」笔记《一平米窗边书桌改造避坑》", xhs_line)
        self.assertIn("加了收藏", xhs_line)
        self.assertNotIn("未投币", xhs_line)
        self.assertNotIn("B站UP主", xhs_line)

        dy_item = {
            "item_id": "dy_01",
            "kind": "reading",
            "source": "douyin",
            "ref": "https://www.douyin.com/video/7399001",
            "author": "夜宵摊老陈",
            "title": "铁板炒饭为什么家里炒不出锅气",
            "text": "刷抖音看到有人拿测温枪量铁板炒饭的锅温",
            "actions_json": '{"like": true, "follow": true}',
            "_matched": True,
        }
        self.assertEqual(life_line.detect_item_platform(dy_item), "douyin")
        dy_line = life_line.render_line([dy_item], asked=True)
        self.assertIn("来源：抖音创作者「夜宵摊老陈」短视频《铁板炒饭为什么家里炒不出锅气》", dy_line)
        self.assertIn("关注了这位创作者", dy_line)
        self.assertNotIn("B站UP主", dy_line)


if __name__ == "__main__":
    unittest.main()
