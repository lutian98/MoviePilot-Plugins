"""
EmbySyncDel 单元测试。

覆盖插件最容易出错、也是本插件存在理由的部分：

- 事件名过滤（媒体服务器事件名不一致是同类插件整体失效的第一大原因）
- 路径映射与"路径是否在允许范围内"
- **后缀无关匹配**：媒体服务器上报 .strm、整理记录里是 .mkv/.mp4 时必须能对上
- 演练模式与防误删校验

运行：``python -m unittest discover -s tests`` 或 ``pytest``
"""

import unittest

from tests import app_stub

plugin = app_stub.load_plugin()


class FakeRecord:
    """整理记录桩。"""

    def __init__(self, rid, title, dest, src=None, download_hash=None, image=None):
        self.id = rid
        self.title = title
        self.dest = dest
        self.src = src
        self.download_hash = download_hash
        self.image = image
        self.date = None


class FakeTransferHis:
    """整理记录操作桩：可控制查询结果并记录删除动作。"""

    def __init__(self, records=None):
        self.records = records or []
        self.deleted = []
        self.queries = []

    def query(self, filters, page):
        self.queries.append(filters)
        return self.records, len(self.records)

    def delete(self, historyid):
        self.deleted.append(historyid)
        return True


def build_plugin(records=None, dry_run=True, library_path="/mnt/user/Media/Strm/Media#/downloads/link",
                 exclude_path="", title=None):
    """构造一个可测试的插件实例。"""
    instance = plugin.EmbySyncDel()
    notifies = []
    instance._enabled = True
    instance._notify = True
    instance._dry_run = dry_run
    instance._event_types = plugin.DEFAULT_EVENT_TYPES
    instance._library_path = library_path
    instance._exclude_path = exclude_path
    instance._del_source = True
    instance._del_seed = True
    instance._torrent_action = plugin.ACTION_DELETE
    instance._title_guard = True
    instance._transferhis = FakeTransferHis(records)
    instance._downloadhis = None
    instance._downloader_helper = None
    instance._default_downloader = None
    instance._notify_result = lambda **kwargs: notifies.append(kwargs)
    return instance, notifies


def make_event(event="library.deleted", name="微微一笑很倾城", path=None,
               media_type="Movie", media_id="412190", provider_ids=None):
    """构造媒体服务器事件桩。"""
    path = path or ("/mnt/user/Media/Strm/Media/Movie/中国电影/微微一笑很倾城 (2016)/"
                    "微微一笑很倾城 (2016) - 1080p.strm")
    payload = {}
    if provider_ids is not None:
        payload = {"Item": {"ProviderIds": provider_ids}}
    return app_stub.WebhookEventInfo(
        event=event,
        item_name=name,
        item_path=path,
        media_type=media_type,
        item_type=media_type,
        media_source=app_stub.MediaSource.TMDB if media_id else None,
        media_id=media_id,
        json_object=payload,
    )


class TestEventTypes(unittest.TestCase):
    """事件名解析。"""

    def test_parse_default(self):
        self.assertEqual(
            plugin.parse_event_types(plugin.DEFAULT_EVENT_TYPES),
            ["library.deleted", "ItemDeleted", "item.deleted"],
        )

    def test_parse_blank(self):
        self.assertEqual(plugin.parse_event_types(None), [])
        self.assertEqual(plugin.parse_event_types(" , , "), [])

    def test_trim(self):
        self.assertEqual(plugin.parse_event_types(" a , b "), ["a", "b"])


class TestMappings(unittest.TestCase):
    """路径映射解析。"""

    def test_hash_separator(self):
        self.assertEqual(
            plugin.parse_mappings("/mnt/user/Media/Strm/Media#/downloads/link"),
            [("/mnt/user/Media/Strm/Media", "/downloads/link")],
        )

    def test_colon_separator(self):
        self.assertEqual(
            plugin.parse_mappings("/mnt/user/Media/Strm/Media:/downloads/link"),
            [("/mnt/user/Media/Strm/Media", "/downloads/link")],
        )

    def test_trailing_slash_and_blank_lines(self):
        self.assertEqual(
            plugin.parse_mappings("\n\n /a/b/ # /c/d/ \n"),
            [("/a/b", "/c/d")],
        )

    def test_comment_line_ignored(self):
        self.assertEqual(plugin.parse_mappings("# 注释\n/a#/b"), [("/a", "/b")])

    def test_long_prefix_first(self):
        mappings = plugin.parse_mappings("/a#/x\n/a/b#/y")
        self.assertEqual(mappings[0], ("/a/b", "/y"))


class TestMapPath(unittest.TestCase):
    """路径映射。"""

    def setUp(self):
        self.mappings = plugin.parse_mappings("/mnt/user/Media/Strm/Media#/downloads/link\n/a#/x")

    def test_hit(self):
        self.assertEqual(
            plugin.map_path("/mnt/user/Media/Strm/Media/Movie/a/b.strm", self.mappings),
            "/downloads/link/Movie/a/b.strm",
        )

    def test_exact_root(self):
        self.assertEqual(plugin.map_path("/a", self.mappings), "/x")

    def test_no_hit_keeps_original(self):
        self.assertEqual(plugin.map_path("/other/path/c.mkv", self.mappings), "/other/path/c.mkv")

    def test_prefix_should_not_partially_match(self):
        """ /abc 不应命中映射 /a 。"""
        self.mappings = plugin.parse_mappings("/a#/x")
        self.assertEqual(plugin.map_path("/abc/d.mkv", self.mappings), "/abc/d.mkv")

    def test_windows_backslash(self):
        mappings = plugin.parse_mappings("D:/Media#/downloads/link")
        self.assertEqual(plugin.map_path("D:\\Media\\Movie\\a.mkv", mappings),
                         "/downloads/link/Movie/a.mkv")


class TestSameMedia(unittest.TestCase):
    """后缀无关匹配（本插件的核心修复点）。"""

    def test_strm_vs_mkv_same_dir(self):
        """媒体服务器报 .strm、记录里是 .mkv：同目录即同一部片。"""
        record = "/downloads/link/Movie/中国电影/微微一笑很倾城 (2016)/微微一笑很倾城 (2016) - 1080p.mkv"
        event = "/downloads/link/Movie/中国电影/微微一笑很倾城 (2016)/微微一笑很倾城 (2016) - 1080p.strm"
        self.assertTrue(plugin.same_media(record, event, is_tv=False))

    def test_strm_vs_mp4_different_resolution_in_same_dir(self):
        """同目录多版本（1080p / 2160p）全部命中。"""
        record = "/downloads/link/Movie/中国电影/某片 (2016)/某片 (2016) - 2160p.mp4"
        event = "/downloads/link/Movie/中国电影/某片 (2016)/某片 (2016) - 1080p.strm"
        self.assertTrue(plugin.same_media(record, event, is_tv=False))

    def test_directory_path_event_does_not_match(self):
        """事件路径是「目录」时**不得**命中该目录下的记录。

        依据（2026-10-08 实测生产日志）：633 条 Movie 型删除事件全部带文件后缀，0 条目录路径；
        而目录路径恰好来自整理换版重建 —— Emby 先报「文件夹被删」、20 秒后影片又被加回来，
        影片其实一直在库里。靠目录名命中记录会把仍在库的影片连源文件一起删掉且不可逆。
        """
        record = "/downloads/link/Movie/中国电影/某片 (2016)/某片 (2016) - 1080p.mkv"
        event = "/downloads/link/Movie/中国电影/某片 (2016)"
        self.assertFalse(plugin.same_media(record, event, is_tv=False))

    def test_different_movie_not_matched(self):
        record = "/downloads/link/Movie/中国电影/某片 (2016)/某片 (2016) - 1080p.mkv"
        event = "/downloads/link/Movie/中国电影/另一部 (2016)/另一部 (2016) - 1080p.strm"
        self.assertFalse(plugin.same_media(record, event, is_tv=False))

    def test_stem_match_across_dirs(self):
        record = "/downloads/link/Movie/某片 (2016)/某片 (2016) - 1080p.mkv"
        event = "/downloads/link/其他目录/某片 (2016) - 1080p.strm"
        self.assertTrue(plugin.same_media(record, event, is_tv=False))

    def test_tv_episode_same_stem(self):
        record = "/downloads/link/TV/某剧 (2019)/Season 1/某剧 - S01E01 - 1080p.mkv"
        event = "/downloads/link/TV/某剧 (2019)/Season 1/某剧 - S01E01 - 1080p.strm"
        self.assertTrue(plugin.same_media(record, event, is_tv=True))

    def test_tv_should_not_match_other_episode_in_same_dir(self):
        """剧集必须按文件名主干匹配，避免整季误删。"""
        record = "/downloads/link/TV/某剧 (2019)/Season 1/某剧 - S01E05 - 1080p.mkv"
        event = "/downloads/link/TV/某剧 (2019)/Season 1/某剧 - S01E01 - 1080p.strm"
        self.assertFalse(plugin.same_media(record, event, is_tv=True))

    def test_empty_inputs(self):
        self.assertFalse(plugin.same_media(None, "/a/b.mkv", is_tv=False))
        self.assertFalse(plugin.same_media("/a/b.mkv", "", is_tv=False))


class TestIsWithin(unittest.TestCase):
    """删除范围护栏。"""

    def test_inside(self):
        self.assertTrue(plugin.is_within("/downloads/link/a/b.mkv", ["/downloads/link"]))

    def test_equal_root(self):
        self.assertTrue(plugin.is_within("/downloads/link", ["/downloads/link"]))

    def test_prefix_trap(self):
        """ /downloads/link-old 不得被判进 /downloads/link 。"""
        self.assertFalse(plugin.is_within("/downloads/link-old/a.mkv", ["/downloads/link"]))

    def test_outside(self):
        self.assertFalse(plugin.is_within("/etc/passwd", ["/downloads/link"]))

    def test_no_roots(self):
        self.assertFalse(plugin.is_within("/downloads/link/a.mkv", []))


class TestWebhookFlow(unittest.TestCase):
    """事件入口 → 匹配 → 演练/删除 的整链路。"""

    def test_wrong_event_name_is_ignored(self):
        """事件名不在允许清单内时，插件必须完全静默。"""
        instance, notifies = build_plugin([FakeRecord(1, "微微一笑很倾城", "/downloads/link/a.mkv")])
        instance.sync_del_by_webhook(app_stub.Event(event_data=make_event(event="library.new")))
        self.assertEqual(notifies, [])

    def test_default_event_names_accepted(self):
        """默认三种事件名都要被接受（媒体服务器事件名不一致时的容错）。"""
        record_dest = ("/downloads/link/Movie/中国电影/微微一笑很倾城 (2016)/"
                       "微微一笑很倾城 (2016) - 1080p.mkv")
        for name in ("library.deleted", "ItemDeleted", "item.deleted"):
            instance, notifies = build_plugin([FakeRecord(1, "微微一笑很倾城", record_dest)])
            instance.sync_del_by_webhook(app_stub.Event(event_data=make_event(event=name)))
            self.assertEqual(len(notifies), 1, msg=name)
            self.assertIn("演练", notifies[0]["title"])

    def test_strm_path_matches_real_file_record(self):
        """核心回归用例：Emby 报 .strm，整理记录是 .mkv → 仍能命中并出清单。"""
        record_dest = ("/downloads/link/Movie/中国电影/微微一笑很倾城 (2016)/"
                       "微微一笑很倾城 (2016) - 1080p.mkv")
        instance, notifies = build_plugin([FakeRecord(20597, "微微一笑很倾城", record_dest)])
        instance.sync_del_by_webhook(app_stub.Event(event_data=make_event()))
        self.assertEqual(len(notifies), 1)
        self.assertIn("将删除 1 项", notifies[0]["title"])
        self.assertIn(record_dest, notifies[0]["text"])
        self.assertEqual(instance._transferhis.deleted, [])

    def test_no_identity_falls_back_to_path(self):
        """事件缺媒体身份时不再直接放弃：改用路径匹配；路径对不上照样放弃，绝不按标题猜。"""
        instance, notifies = build_plugin([FakeRecord(1, "某片", "/downloads/link/a.mp4")])
        event = make_event(media_id=None, provider_ids=None)
        event.media_source = None
        instance.sync_del_by_webhook(app_stub.Event(event_data=event))
        self.assertEqual(len(notifies), 1)
        self.assertIn("未找到匹配的整理记录", notifies[0]["text"])
        self.assertEqual(instance._transferhis.deleted, [])

    def test_no_identity_path_match_still_works(self):
        """缺身份但路径能对上时，仍然正常命中并出清单（真实 Emby 报文的主要形态）。"""
        record_dest = ("/downloads/link/Movie/中国电影/微微一笑很倾城 (2016)/"
                       "微微一笑很倾城 (2016) - 1080p.mkv")
        instance, notifies = build_plugin([FakeRecord(20597, "微微一笑很倾城", record_dest)])
        event = make_event(media_id=None, provider_ids=None)
        event.media_source = None
        instance.sync_del_by_webhook(app_stub.Event(event_data=event))
        self.assertEqual(len(notifies), 1)
        self.assertIn("将删除 1 项", notifies[0]["title"])

    def test_identity_from_provider_ids(self):
        """事件没带身份时，用报文的 ProviderIds 兜底。"""
        record_dest = "/downloads/link/Movie/中国电影/某片 (2016)/某片 (2016) - 1080p.mkv"
        instance, notifies = build_plugin([FakeRecord(7, "某片", record_dest)])
        event = make_event(name="某片", media_id=None, provider_ids={"Tmdb": "12345"},
                          path="/mnt/user/Media/Strm/Media/Movie/中国电影/某片 (2016)/某片 (2016) - 1080p.strm")
        event.media_source = None
        instance.sync_del_by_webhook(app_stub.Event(event_data=event))
        self.assertEqual(len(notifies), 1)
        self.assertIn("将删除 1 项", notifies[0]["title"])

    def test_excluded_path_skipped(self):
        instance, notifies = build_plugin(
            [FakeRecord(1, "微微一笑很倾城", "/downloads/link/a.mkv")],
            exclude_path="/mnt/user/Media/Strm/Media/Movie/中国电影",
        )
        instance.sync_del_by_webhook(app_stub.Event(event_data=make_event()))
        self.assertEqual(notifies, [])

    def test_no_matching_record_notifies(self):
        instance, notifies = build_plugin([FakeRecord(1, "另一部", "/downloads/link/other/a.mkv")])
        instance.sync_del_by_webhook(app_stub.Event(event_data=make_event()))
        self.assertEqual(len(notifies), 1)
        self.assertIn("未找到", notifies[0]["title"])

    def test_disabled_plugin_does_nothing(self):
        instance, notifies = build_plugin([FakeRecord(1, "微微一笑很倾城", "/downloads/link/a.mkv")])
        instance._enabled = False
        instance.sync_del_by_webhook(app_stub.Event(event_data=make_event()))
        self.assertEqual(notifies, [])


class TestDeleteFlow(unittest.TestCase):
    """关闭演练模式后的真实删除动作（文件不存在，只校验记录与护栏）。"""

    def test_delete_record_and_keep_history_on_failure(self):
        record = FakeRecord(20597, "微微一笑很倾城",
                            "/downloads/link/Movie/中国电影/微微一笑很倾城 (2016)/某片 - 1080p.mkv")
        instance, notifies = build_plugin([record], dry_run=False)
        instance.sync_del_by_webhook(app_stub.Event(event_data=make_event()))
        self.assertEqual(instance._transferhis.deleted, [20597])
        self.assertEqual(len(notifies), 1)
        self.assertIn("联动删除完成", notifies[0]["title"])

    def test_title_guard_blocks_mismatch(self):
        """整理记录标题与删除媒体不符时，防误删必须挡住。"""
        record = FakeRecord(1, "完全不同的片名", "/downloads/link/a/b.mkv")
        instance, notifies = build_plugin([record], dry_run=False)
        instance.sync_del_by_webhook(app_stub.Event(event_data=make_event()))
        self.assertEqual(instance._transferhis.deleted, [])

    def test_media_library_file_outside_mapping_is_skipped(self):
        """媒体库文件不在映射范围内时：跳过文件、保留整理记录以便补删，不做任何删除。"""
        record = FakeRecord(1, "微微一笑很倾城",
                            "/media/Movies/微微一笑很倾城 (2016)/微微一笑很倾城 (2016) - 1080p.mkv")
        instance, notifies = build_plugin([record], dry_run=False, library_path="")
        instance.sync_del_by_webhook(app_stub.Event(event_data=make_event(
            path="/media/Movies/微微一笑很倾城 (2016)/微微一笑很倾城 (2016) - 1080p.strm")))
        self.assertEqual(len(notifies), 1)
        self.assertIn("不在映射范围内", notifies[0]["text"])
        self.assertEqual(instance._transferhis.deleted, [])


class TestLazyGuards(unittest.TestCase):
    """辅助函数。"""

    def test_is_media_file(self):
        self.assertTrue(plugin.is_media_file("/a/b.mkv"))
        self.assertTrue(plugin.is_media_file("/a/b.MP4"))
        self.assertFalse(plugin.is_media_file("/a/b.strm"))
        self.assertFalse(plugin.is_media_file("/a/b"))
        self.assertFalse(plugin.is_media_file(None))

    def test_plugin_metadata(self):
        """插件元数据必须完整（插件市场识别依赖）。"""
        self.assertTrue(plugin.EmbySyncDel.plugin_name)
        self.assertTrue(plugin.EmbySyncDel.plugin_desc)
        self.assertTrue(plugin.EmbySyncDel.plugin_version)
        self.assertTrue(plugin.EmbySyncDel.plugin_author)
        self.assertTrue(plugin.EmbySyncDel.plugin_icon)
        self.assertTrue(plugin.EmbySyncDel.plugin_config_prefix)
        self.assertEqual(plugin.EmbySyncDel.plugin_config_prefix[-1], "_")
        self.assertTrue(plugin.EmbySyncDel.auth_level >= 0)

    def test_registered_event(self):
        """必须注册 WebhookMessage 事件，否则永远收不到删除通知。"""
        self.assertIn(app_stub.EventType.WebhookMessage, app_stub.eventmanager.handlers)


class TestDirectWebhookApi(unittest.TestCase):
    """直投 webhook 入口（媒体服务器把 JSON 直接投给插件）—— 本插件独立工作的关键路径。"""

    def _instance(self, records=None):
        instance, notifies = build_plugin(records)
        instance._webhook_key = "k123"
        return instance, notifies

    def test_rejects_wrong_key(self):
        instance, notifies = self._instance()
        out = instance.api_emby_webhook({"Event": "library.deleted"}, key="bad")
        self.assertFalse(out["ok"])
        self.assertEqual(notifies, [])

    def test_rejects_empty_body(self):
        instance, _ = self._instance()
        self.assertFalse(instance.api_emby_webhook({}, key="k123")["ok"])
        self.assertFalse(instance.api_emby_webhook(None, key="k123")["ok"])
        self.assertFalse(instance.api_emby_webhook({"Item": {"Name": "x"}}, key="k123")["ok"])

    def test_accepts_and_cleans_up(self):
        """Emby 真实报文形状（JSON body、无 ProviderIds）应能命中并按路径清理。"""
        record_dest = ("/downloads/link/Movie/中国电影/微微一笑很倾城 (2016)/"
                       "微微一笑很倾城 (2016) - 1080p.mkv")
        instance, notifies = self._instance([FakeRecord(20597, "微微一笑很倾城", record_dest)])
        payload = {
            "Event": "library.deleted",
            "Item": {
                "Type": "Movie",
                "Name": "微微一笑很倾城",
                "ProductionYear": 2016,
                "Path": "/mnt/user/Media/Strm/Media/Movie/中国电影/微微一笑很倾城 (2016)/"
                        "微微一笑很倾城 (2016) - 1080p.strm",
            },
        }
        out = instance.api_emby_webhook(payload, key="k123")
        self.assertTrue(out["ok"])
        # 处理在后台线程，等它跑完
        for _ in range(100):
            if notifies:
                break
            import time as _t
            _t.sleep(0.05)
        self.assertEqual(len(notifies), 1)
        self.assertIn("将删除 1 项", notifies[0]["title"])

    def test_provider_ids_used_when_present(self):
        """报文带 ProviderIds 时应取到 tmdb 身份（不再依赖路径兜底）。"""
        instance, _ = self._instance()
        data = instance._event_data_from_payload({
            "Event": "library.deleted",
            "Item": {"Type": "Movie", "Name": "某片", "ProductionYear": 2016,
                     "ProviderIds": {"Tmdb": "12345"}},
        })
        self.assertEqual(str(data.media_id), "12345")
        self.assertEqual(data.item_name, "某片 (2016)")
        self.assertEqual(data.event, "library.deleted")


class TestNotificationImage(unittest.TestCase):
    """通知配图：优先用整理记录自带海报（老插件的做法），没有才用兜底图标。"""

    def test_poster_from_record(self):
        poster = "https://image.tmdb.org/t/p/w500/abc123.jpg"
        instance, _ = build_plugin([FakeRecord(1, "某片", "/x/某片.mkv", image=poster)])
        self.assertEqual(instance._poster_image([FakeRecord(1, "某片", "/x/某片.mkv", image=poster)]), poster)

    def test_fallback_when_no_image(self):
        instance, _ = build_plugin([FakeRecord(1, "某片", "/x/某片.mkv")])
        self.assertEqual(
            instance._poster_image([FakeRecord(1, "某片", "/x/某片.mkv")]),
            plugin.EmbySyncDel._fallback_image(),
        )

    def test_fallback_on_bad_url(self):
        """非 http(s) 的值（例如本地相对路径）不予采用，避免通知里出现无效图。"""
        instance, _ = build_plugin()
        self.assertEqual(
            instance._poster_image([FakeRecord(1, "某片", "/x/某片.mkv", image="/local/poster.jpg")]),
            plugin.EmbySyncDel._fallback_image(),
        )

    def test_notification_carries_poster(self):
        """端到端：演练通知的图片应是海报而不是兜底图标。"""
        poster = "https://image.tmdb.org/t/p/w500/xyz.jpg"
        rec = FakeRecord(20597, "微微一笑很倾城", "/downloads/link/Movie/中国电影/微微一笑很倾城 (2016)/某片.mkv",
                         image=poster)
        instance, notifies = build_plugin([rec])
        instance._webhook_key = "k123"
        payload = {
            "Event": "library.deleted",
            "Item": {"Type": "Movie", "Name": "微微一笑很倾城", "ProductionYear": 2016,
                     "Path": "/mnt/user/Media/Strm/Media/Movie/中国电影/微微一笑很倾城 (2016)/某片.strm"},
        }
        self.assertTrue(instance.api_emby_webhook(payload, key="k123")["ok"])
        import time as _t
        for _ in range(100):
            if notifies:
                break
            _t.sleep(0.05)
        self.assertEqual(len(notifies), 1)
        self.assertEqual(notifies[0].get("image"), poster)


class TestContainerEventGuard(unittest.TestCase):
    """容器级事件（文件夹/剧集容器）默认必须拒绝处理。

    生产事故样本（2026-10-08）：Emby 在整理换版时先报「文件夹被删」、20 秒后影片又被加回来，
    影片其实一直在库里；若按目录名匹配到整理记录就删，会把仍在库的影片连源文件一起删（不可逆）。
    """

    FOLDER_PAYLOAD = {
        "Event": "library.deleted",
        "Item": {"Type": "Folder", "Name": "马戏之王 (2017)",
                 "Path": "/mnt/user/Media/Strm/Media/Movie/欧美电影/马戏之王 (2017)"},
    }

    def test_folder_event_is_skipped(self):
        rec = FakeRecord(99001, "马戏之王",
                         "/downloads/link/Movie/欧美电影/马戏之王 (2017)/马戏之王 (2017) - 1080p.mkv")
        instance, notifies = build_plugin([rec])
        instance.save_data("history", [])          # 清掉其它用例留下的历史
        instance._webhook_key = "k"
        instance._dry_run = False          # 即使是真实删除模式，也必须拒绝
        out = instance.api_emby_webhook(self.FOLDER_PAYLOAD, key="k")
        self.assertTrue(out["ok"])          # 收下事件，但不处理
        import time as _t
        _t.sleep(0.6)
        self.assertEqual(notifies, [])      # 不打扰用户
        stages = [x.get("stage") for x in (instance.get_data("history") or [])]
        self.assertIn("skipped_container", stages)
        self.assertNotIn("done", stages)    # 绝不执行删除
        self.assertEqual(instance._transferhis.deleted, [])

    def test_series_container_also_skipped(self):
        instance, notifies = build_plugin()
        instance.save_data("history", [])
        instance._webhook_key = "k"
        payload = {"Event": "library.deleted",
                   "Item": {"Type": "Series", "Name": "某剧", "Path": "/mnt/user/Media/Strm/Media/TV/某剧"}}
        self.assertTrue(instance.api_emby_webhook(payload, key="k")["ok"])
        import time as _t
        _t.sleep(0.6)
        stages = [x.get("stage") for x in (instance.get_data("history") or [])]
        self.assertIn("skipped_container", stages)

    def test_movie_type_event_with_directory_path_does_not_match(self):
        """纵深防御：即使事件自称 Movie，只要路径是「目录」就不能按目录级规则命中记录。

        整理换版重建时 Emby 可能报出这类报文；若命中就会把仍在库里的影片删掉。
        """
        rec = FakeRecord(99004, "马戏之王",
                         "/downloads/link/Movie/欧美电影/马戏之王 (2017)/马戏之王 (2017) - 1080p.mkv")
        instance, notifies = build_plugin([rec])
        instance.save_data("history", [])
        instance._webhook_key = "k"
        payload = {"Event": "library.deleted",
                   "Item": {"Type": "Movie", "Name": "马戏之王 (2017)",
                            "Path": "/mnt/user/Media/Strm/Media/Movie/欧美电影/马戏之王 (2017)"}}
        self.assertTrue(instance.api_emby_webhook(payload, key="k")["ok"])
        import time as _t
        for _ in range(60):
            if notifies:
                break
            _t.sleep(0.05)
        # 不删除，只告警（Movie 型事件匹配不到记录时提醒用户，这类事件生产环境极少见）
        self.assertEqual(len(notifies), 1)
        self.assertIn("未找到整理记录", notifies[0]["title"])
        self.assertEqual(instance._transferhis.deleted, [])
        stages = [x.get("stage") for x in (instance.get_data("history") or [])]
        self.assertIn("no_record", stages)

    def test_movie_event_still_processed(self):
        """回归：影片文件级事件不受影响，照常处理。"""
        rec = FakeRecord(99003, "马戏之王",
                         "/downloads/link/Movie/欧美电影/马戏之王 (2017)/马戏之王 (2017) - 1080p.mkv")
        instance, notifies = build_plugin([rec])
        instance.save_data("history", [])
        instance._webhook_key = "k"
        payload = {"Event": "library.deleted",
                   "Item": {"Type": "Movie", "Name": "马戏之王", "ProductionYear": 2017,
                            "Path": "/mnt/user/Media/Strm/Media/Movie/欧美电影/马戏之王 (2017)/"
                                    "马戏之王 (2017) - 1080p.strm"}}
        self.assertTrue(instance.api_emby_webhook(payload, key="k")["ok"])
        import time as _t
        for _ in range(100):
            if notifies:
                break
            _t.sleep(0.05)
        self.assertEqual(len(notifies), 1)
        self.assertIn("演练", notifies[0]["title"])


if __name__ == "__main__":
    unittest.main()
