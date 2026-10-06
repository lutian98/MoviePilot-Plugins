"""
EmbySyncDel 端到端沙箱测试。

在**真实文件系统**上仿真一套与生产同构的环境：

```
沙箱根/
├── emby/    媒体服务器侧媒体库（strm 占位文件）
├── library/ MoviePilot 侧媒体库（真实媒体文件）
└── downloads/PT/保种/  源文件（做种目录）
```

配合桩掉的宿主（整理记录 Oper、下载器 Chain、转种 / 辅种插件数据），完整跑通
「删除事件 → 身份解析 → 路径映射 → 后缀无关匹配 → 删文件 → 删空目录 → 删种子
（主任务 + 转种 + 辅种）→ 删整理记录」全链路，并验证隔离性：

- 同目录的其它影片必须毫发无损
- 演练模式必须一个字节都不动
- 源文件目录被清理时，上层非空目录必须保留
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from tests import app_stub

plugin = app_stub.load_plugin()

STRM_SUFFIX = ".strm"
MEDIA_SUFFIX = ".mkv"
HASH_MAIN = "aaa111"
HASH_TRANSFER = "bbb222"
HASH_SEED = "ccc333"


class FakeRecord:
    """整理记录桩。"""

    def __init__(self, rid, title, dest, src=None, download_hash=None):
        self.id = rid
        self.title = title
        self.dest = dest
        self.src = src
        self.download_hash = download_hash
        self.date = None


class FakeTransferHis:
    """整理记录操作桩：返回全部记录，由插件自己做匹配与护栏判断。"""

    def __init__(self, records):
        self.records = records
        self.deleted = []

    def query(self, filters, page):
        return list(self.records), len(self.records)

    def delete(self, historyid):
        self.deleted.append(historyid)
        return True


class Sandbox:
    """一套完整的仿真环境。"""

    def __init__(self, dry_run=True, torrent_action="delete", title_guard=True):
        self.root = Path(tempfile.mkdtemp(prefix="embysyncdel-"))
        self.emby = self.root / "emby"
        self.library = self.root / "library"
        self.downloads = self.root / "downloads"

        # 媒体服务器侧：strm 占位
        self.emby_main = self.emby / "Movie" / "中国电影" / "微微一笑很倾城 (2016)"
        self.emby_other = self.emby / "Movie" / "中国电影" / "另一部电影 (2017)"
        self.emby_strm = self.emby_main / f"微微一笑很倾城 (2016) - 1080p{STRM_SUFFIX}"
        self.emby_other_strm = self.emby_other / f"另一部电影 (2017) - 1080p{STRM_SUFFIX}"

        # MoviePilot 侧：真实媒体文件
        self.lib_main = self.library / "Movie" / "中国电影" / "微微一笑很倾城 (2016)"
        self.lib_other = self.library / "Movie" / "中国电影" / "另一部电影 (2017)"
        self.lib_file = self.lib_main / f"微微一笑很倾城 (2016) - 1080p{MEDIA_SUFFIX}"
        self.lib_other_file = self.lib_other / f"另一部电影 (2017) - 1080p{MEDIA_SUFFIX}"

        # 源文件（做种目录）：去掉年份的多余层级，模拟真实保种目录
        self.src_main_file = (self.downloads / "PT" / "保种" / "微微一笑很倾城"
                              / f"微微一笑很倾城 (2016) - 1080p{MEDIA_SUFFIX}")
        self.src_other_file = (self.downloads / "PT" / "保种" / "另一部电影"
                               / f"另一部电影 (2017) - 1080p{MEDIA_SUFFIX}")

        for path in (self.emby_strm, self.emby_other_strm, self.lib_file, self.lib_other_file,
                     self.src_main_file, self.src_other_file):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("placeholder\n" if path.suffix == STRM_SUFFIX else "video\n",
                            encoding="utf-8")

        app_stub.reset()
        app_stub.PLUGIN_DATA[("TorrentTransfer", "QB-aaa111")] = {
            "to_download": "TR",
            "to_download_id": HASH_TRANSFER,
            "from_download": "QB",
            "from_download_id": HASH_MAIN,
        }
        app_stub.PLUGIN_DATA[("IYUUAutoSeed", HASH_MAIN)] = [
            {"downloader": "QB", "torrents": [HASH_SEED]}
        ]

        self.records = [
            FakeRecord(20597, "微微一笑很倾城", str(self.lib_file), str(self.src_main_file),
                       HASH_MAIN),
            FakeRecord(20598, "另一部电影", str(self.lib_other_file), str(self.src_other_file),
                       "ddd444"),
        ]
        self.transferhis = FakeTransferHis(self.records)

        self.instance = plugin.EmbySyncDel()
        self.notifies = []
        self.instance._enabled = True
        self.instance._notify = True
        self.instance._dry_run = dry_run
        self.instance._event_types = plugin.DEFAULT_EVENT_TYPES
        self.instance._library_path = f"{self.emby}#{self.library}"
        self.instance._exclude_path = ""
        self.instance._del_source = True
        self.instance._del_seed = True
        self.instance._torrent_action = torrent_action
        self.instance._title_guard = title_guard
        self.instance._transferhis = self.transferhis
        self.instance._downloadhis = app_stub.FakeDownloadHistoryOper({HASH_MAIN: "QB"})
        self.instance._downloader_helper = None
        self.instance._default_downloader = None
        self.instance._notify_result = lambda **kwargs: self.notifies.append(kwargs)
        self.chain = app_stub.FakeChain()
        self.instance.chain = self.chain

    def event(self, path=None, name="微微一笑很倾城", event="library.deleted"):
        """构造媒体服务器删除事件。"""
        return app_stub.Event(event_data=app_stub.WebhookEventInfo(
            event=event,
            item_name=name,
            item_path=str(path or self.emby_strm),
            media_type="Movie",
            item_type="Movie",
            media_source=app_stub.MediaSource.TMDB,
            media_id="412190",
            json_object={"Item": {"ProviderIds": {"Tmdb": "412190"}}},
        ))

    def fire(self, **kwargs):
        """触发一次删除事件。"""
        self.instance.sync_del_by_webhook(self.event(**kwargs))

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class TestSandboxDryRun(unittest.TestCase):
    """演练模式：只出清单，不碰任何东西。"""

    def setUp(self):
        self.sbx = Sandbox(dry_run=True)

    def tearDown(self):
        self.sbx.cleanup()

    def test_nothing_is_deleted(self):
        self.sbx.fire()
        for path in (self.sbx.emby_strm, self.sbx.lib_file, self.sbx.src_main_file,
                     self.sbx.emby_other_strm, self.sbx.lib_other_file):
            self.assertTrue(path.exists(), msg=str(path))
        self.assertEqual(self.sbx.transferhis.deleted, [])
        self.assertEqual(self.sbx.chain.removed, [])
        self.assertEqual(self.sbx.chain.stopped, [])

    def test_plan_lists_exactly_one_record(self):
        self.sbx.fire()
        self.assertEqual(len(self.sbx.notifies), 1)
        text = self.sbx.notifies[0]["text"]
        self.assertIn("将删除 1 项", self.sbx.notifies[0]["title"])
        self.assertIn(str(self.sbx.lib_file), text)
        self.assertIn(str(self.sbx.src_main_file), text)
        self.assertIn(str(self.sbx.emby_strm), text)
        self.assertNotIn("另一部电影", text)


class TestSandboxRealDelete(unittest.TestCase):
    """关闭演练后的真实删除：文件、空目录、种子、整理记录全链路。"""

    def setUp(self):
        self.sbx = Sandbox(dry_run=False)

    def tearDown(self):
        self.sbx.cleanup()

    def test_files_and_placeholder_removed(self):
        self.sbx.fire()
        self.assertFalse(self.sbx.lib_file.exists(), "媒体库文件应被删除")
        self.assertFalse(self.sbx.src_main_file.exists(), "源文件应被删除")
        self.assertFalse(self.sbx.emby_strm.exists(), "strm 占位文件应被删除")

    def test_empty_dirs_pruned_but_non_empty_parent_kept(self):
        self.sbx.fire()
        self.assertFalse(self.sbx.lib_main.exists(), "空的影片目录应被清理")
        self.assertTrue(self.sbx.lib_other.exists(), "同目录的其它影片必须保留")
        self.assertTrue(self.sbx.lib_other_file.exists(), "同目录的其它影片文件必须保留")
        self.assertTrue((self.sbx.library / "Movie" / "中国电影").exists(),
                        "非空的上级目录必须保留")
        self.assertFalse((self.sbx.downloads / "PT" / "保种" / "微微一笑很倾城").exists())
        self.assertTrue((self.sbx.downloads / "PT" / "保种").exists(), "非空上级目录必须保留")

    def test_sibling_film_untouched(self):
        self.sbx.fire()
        self.assertTrue(self.sbx.emby_other_strm.exists(), "其它影片的占位文件不应被动")
        self.assertTrue(self.sbx.src_other_file.exists(), "其它影片的源文件不应被动")

    def test_torrents_removed_including_transfer_and_seed(self):
        self.sbx.fire()
        hashes = {item["hashs"] for item in self.sbx.chain.removed}
        self.assertEqual(hashes, {HASH_MAIN, HASH_TRANSFER, HASH_SEED})
        self.assertTrue(all(item["delete_file"] for item in self.sbx.chain.removed))
        self.assertEqual(self.sbx.chain.stopped, [])

    def test_record_deleted_last(self):
        self.sbx.fire()
        self.assertEqual(self.sbx.transferhis.deleted, [20597])

    def test_notification_summarises_success(self):
        self.sbx.fire()
        self.assertEqual(len(self.sbx.notifies), 1)
        self.assertIn("联动删除完成", self.sbx.notifies[0]["title"])
        self.assertIn("成功处理 1 条，失败 0 条", self.sbx.notifies[0]["text"])


class TestSandboxStopAction(unittest.TestCase):
    """下载任务处理方式 = 暂停：只停种子，不动下载器数据。"""

    def test_stop_instead_of_remove(self):
        sbx = Sandbox(dry_run=False, torrent_action=plugin.ACTION_STOP)
        try:
            sbx.fire()
            self.assertEqual(sbx.chain.removed, [])
            hashes = {item["hashs"] for item in sbx.chain.stopped}
            self.assertEqual(hashes, {HASH_MAIN, HASH_TRANSFER, HASH_SEED})
            self.assertFalse(sbx.lib_file.exists())
        finally:
            sbx.cleanup()


class TestSandboxGuards(unittest.TestCase):
    """护栏：标题不符时保留记录、保留文件，便于人工复核。"""

    def test_title_guard_blocks_and_keeps_record(self):
        sbx = Sandbox(dry_run=False, title_guard=True)
        try:
            sbx.records[0].title = "片名对不上"
            sbx.fire()
            self.assertEqual(sbx.transferhis.deleted, [])
            self.assertTrue(sbx.lib_file.exists(), "标题不符时必须保留文件")
            self.assertEqual(sbx.chain.removed, [])
        finally:
            sbx.cleanup()

    def test_missing_library_file_is_tolerated(self):
        """媒体库文件已经被手工删掉时，其余清理照常完成。"""
        sbx = Sandbox(dry_run=False)
        try:
            sbx.lib_file.unlink()
            sbx.fire()
            self.assertEqual(sbx.transferhis.deleted, [20597])
            self.assertFalse(sbx.src_main_file.exists())
            self.assertEqual({item["hashs"] for item in sbx.chain.removed},
                             {HASH_MAIN, HASH_TRANSFER, HASH_SEED})
        finally:
            sbx.cleanup()


if __name__ == "__main__":
    unittest.main()
