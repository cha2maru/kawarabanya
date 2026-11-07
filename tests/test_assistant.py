from kawarabanya.bot import assistant
from kawarabanya.core import db
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


def test_opml_and_auto_genre(tmp_path):
    conn, genres = make(tmp_path)
    it = ask(conn, genres, "これ取り込んで", command="opml_import")
    assert it.error  # 添付がない
    it = assistant.build({"command": "opml_import", "genre": ""}, "これ取り込んで", conn, genres, "data/uploads/1_a.opml")
    assert it.argv == ["import-opml", "data/uploads/1_a.opml", "--genre", "auto"] and it.confirm
    it = assistant.build({"command": "opml_export"}, "OPML ちょうだい", conn, genres, export_path="out.opml")
    assert it.argv == ["export-opml", "out.opml"] and it.attach == "out.opml" and not it.confirm
    # ジャンルを言わずに追加 → LLM に決めさせる
    it = ask(conn, genres, "https://example.com/ を追加して", command="feed_add")
    assert it.argv == ["feeds", "add", "https://example.com/", "--genre", "auto"]


def test_guess_genre_options(tmp_path):
    import asyncio
    import json
    import math

    import httpx

    from kawarabanya.core.config import SummarizeConfig
    from kawarabanya.core.llm import Summarizer
    from kawarabanya.rss import placement

    _, genres = make(tmp_path)
    seen = []

    def llm(req):
        body = json.loads(req.content)
        seen.append(body["messages"][0]["content"])
        top = [{"token": "B", "logprob": math.log(0.9)}, {"token": "A", "logprob": math.log(0.1)}]
        return httpx.Response(200, json={"choices": [{"message": {"content": "B"},
                                                      "logprobs": {"content": [{"top_logprobs": top}]}}]})

    async def go(titles):
        async with Summarizer(SummarizeConfig(), transport=httpx.MockTransport(llm)) as s:
            return await placement.guess_genre(s, genres, "サイト", titles)

    assert asyncio.run(go(["プラモの新作"])) == "hobby"  # B = ホビー
    assert "B: ホビー（例: VAPE）" in seen[0]  # サブジャンルの名前を手がかりに添える
    assert asyncio.run(go([])) == "hobby"  # 見出しが取れなくてもサイト名で選ぶ


def test_feed_interest_intent(tmp_path):
    conn, genres = make(tmp_path)
    it = ask(conn, genres, "GIGAZINE は優先度下げて", command="feed_interest", feed="gigazine", level="down")
    assert it.argv == ["feeds", "option", "1", "interest", "down"] and it.confirm
    assert ask(conn, genres, "x", command="feed_interest", feed="gigazine", level="normal").argv == ["feeds", "option", "1", "interest"]
    assert ask(conn, genres, "x", command="feed_interest", feed="nothing", level="down").error


def post_row(conn):
    conn.execute("INSERT OR IGNORE INTO items (id, feed_id, key, title, fetched_at, status, discord_thread_id, subgenre)"
                 " VALUES (5, 1, 'k', 'ゴジラの新作', 'x', 'notified', '777', 'tech')")
    return assistant.find_post(conn, 777)


def test_genre_assign(tmp_path):
    conn, genres = make(tmp_path)
    word = ask(conn, genres, "Ｃｌｏｕｄｆｌａｒｅはtech", command="genre_assign", genre="tech", word="Ｃｌｏｕｄｆｌａｒｅ")
    assert word.argv == ["genres", "keyword", "Cloudflare", "tech"] and word.confirm
    # 投稿のスレッドでは、その記事を移し、語があれば一緒に登録する
    data = {"command": "genre_assign", "genre": "hobby/vape", "word": "これ"}
    move = assistant.build(data, "これはVAPE", conn, genres, post=post_row(conn))
    assert move.argv == ["items", "move", "5", "hobby/vape"] and "引き継げません" in move.summary
    data = {"command": "genre_assign", "genre": "hobby/vape", "word": "ゴジラ"}
    assert assistant.build(data, "ゴジラはVAPE", conn, genres, post=post_row(conn)).argv[-2:] == ["--keyword", "ゴジラ"]
    # 依頼にない語、ジャンルの名前そのものは語にしない。スレッドの外で語がなければ断る
    assert ask(conn, genres, "これはVAPE", command="genre_assign", genre="hobby/vape", word="VAPE").error
    assert ask(conn, genres, "これはテック", command="genre_assign", genre="tech", word="Cloudflare").error
    assert ask(conn, genres, "Cloudflare", command="genre_assign", word="Cloudflare").error
    assert assistant.find_post(conn, 1) is None


def test_genre_written_in_request_wins(tmp_path):
    conn, genres = make(tmp_path)
    data = {"command": "genre_assign", "genre": "hobby/vape", "word": ""}
    assert assistant.build(data, "これはtech", conn, genres, post=post_row(conn)).argv == ["items", "move", "5", "tech"]
    assert assistant.build(data, "これはホビー", conn, genres, post=post_row(conn)).argv[-1] == "hobby"
    assert assistant.build(data, "これは煙のやつ", conn, genres, post=post_row(conn)).argv[-1] == "hobby/vape"
