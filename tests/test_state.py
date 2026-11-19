from gdrivesync.state import Item, StateDB


def test_upsert_and_lookup():
    db = StateDB(":memory:")
    item = Item("f1", "root", "a.txt", "a.txt", False, "text/plain", md5="abc", size=3)
    db.upsert(item)
    assert db.by_id("f1") == item
    assert db.by_path("a.txt") == item

    item.name = item.rel_path = "b.txt"
    db.upsert(item)
    assert db.by_path("a.txt") is None
    assert db.by_path("b.txt").file_id == "f1"
    assert [i.file_id for i in db.children("root")] == ["f1"]

    db.delete("f1")
    assert db.all() == []


def test_page_token():
    db = StateDB(":memory:")
    assert db.page_token is None
    db.page_token = "123"
    assert db.page_token == "123"
