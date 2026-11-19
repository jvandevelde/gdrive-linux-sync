from gdrivesync import names


def test_slash_roundtrip():
    local = names.to_local("Q1/Q2 report")
    assert "/" not in local
    assert names.to_remote(local) == "Q1/Q2 report"


def test_dot_names_are_safe():
    assert names.to_local("..") not in (".", "..")
    assert names.to_local("") == "_"


def test_dedupe():
    assert names.dedupe("a.txt", set()) == "a.txt"
    assert names.dedupe("a.txt", {"a.txt"}) == "a (2).txt"
    assert names.dedupe("a.txt", {"a.txt", "a (2).txt"}) == "a (3).txt"


def test_stub_name():
    assert names.stub_name("Notes", "application/vnd.google-apps.document") == "Notes.gdoc"
    assert names.stub_name("X", "application/vnd.google-apps.unknown") == "X.glink"
