import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from whale_core.memory.group_refresh import enable, export_rows, refresh, stage, status
from whale_core.memory.group_preferences import saved_profile
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.memory.store import MemoryKind


def test_group_messages_wait_for_manual_refresh_and_do_not_duplicate(tmp_path):
    gid = "123"
    archive = tmp_path / "group_archive.sqlite3"
    roster = dict(group_id=gid,name="虚构跑团",description="",members=[
        dict(name="甲",accounts=["101","102"],aliases=[])],bots=[
        dict(name="骰子",accounts=["103"],aliases=[])])
    with closing(SqliteMemoryStore(tmp_path / "memory.sqlite3")) as store:
        enable(store,archive,gid,roster)
        at=datetime.now(timezone.utc).isoformat()
        first=("m1",at,"101","甲","我喜欢《星图》","","")
        same_words=("m2",at,"102","甲","我喜欢《星图》","","")
        assert stage(store,archive,gid,[first,first,same_words])==2
        with closing(sqlite3.connect(archive)) as db:
            assert status(db,gid)==2
        assert saved_profile(store,gid,"101") is None
        assert refresh(store,archive,gid)["processed"]==2
        assert refresh(store,archive,gid)["processed"]==0
        assert saved_profile(store,gid,"101").content["text_count"]==2
        assert len(store.list_by_kind(MemoryKind.PREFERENCE))==1
        assert stage(store,archive,gid,[first])==0
