from kawarabanya import assistant, db
from kawarabanya.rss.genres import load_genres


def make(tmp_path):
    p = tmp_path / "genres.toml"
    p.write_text('[tech]\nlabel = "テック"\n[hobby]\nlabel = "ホビー"\n[hobby.vape]\nlabel = "VAPE"\n', encoding="utf-8")
    conn = db.connect(":memory:")
    db.insert_feed(conn, "https://gigazine.net/news/rss_2.0/", "GIGAZINE", None, "tech")
    db.insert_feed(conn, "https://gizmodo.jp/index.xml", "ギズモード", None, "tech")
    return conn, load_genres(p)


def ask(conn, genres, text, **data):
    return assistant.build({"command": "unknown", "url": "", "genre": "", "feed": "", "level": "", **data},
                           text, conn, genres)


def test_build_intents(tmp_path):
    conn, genres = make(tmp_path)
    # URL は依頼の文から取る
    it = ask(conn, genres, "https://example.com/news」をVAPEに追加して", command="feed_add", url="https://exampl.com", genre="hobby/vape")
    assert it.argv == ["feeds", "add", "https://example.com/news", "--genre", "hobby/vape"] and it.confirm
    assert ask(conn, genres, "追加して", command="feed_add").error
    assert ask(conn, genres, "GIGAZINE を外して", command="feed_remove", feed="gigazine").argv == ["feeds", "remove", "1"]
    assert "2 件" in ask(conn, genres, "g を外して", command="feed_remove", feed="g").error  # 候補が複数
    # サイトが見つからずジャンルの名前なら、ミュートにする
    it = ask(conn, genres, "VAPEの記事はもういらない", command="feed_remove", feed="hobby/vape")
    assert it.argv == ["genres", "interest", "hobby/vape", "mute"] and it.confirm
    it = ask(conn, genres, "x", command="genre_interest", genre="tech", level="like")
    assert it.argv == ["genres", "interest", "tech", "like"]
    assert ask(conn, genres, "x", command="genre_interest", genre="tech").error  # level がない
    assert not ask(conn, genres, "x", command="status").confirm
    assert ask(conn, genres, "x", command="digest_now", genre="tech").argv == ["digest", "--genre", "tech"]
    assert ask(conn, genres, "天気は？").argv == [] and "頼めます" in ask(conn, genres, "?").summary
    assert assistant.build(None, "x", conn, genres).command == "unknown"  # LLM に繋がらない
