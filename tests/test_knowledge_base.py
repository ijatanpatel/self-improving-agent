from support_bot.knowledge_base import load_chunks


def test_one_chunk_per_section():
    chunks = load_chunks()
    ids = [c.id for c in chunks]
    assert len(ids) == len(set(ids)), "chunk ids must be unique"
    assert "account-and-login#1" in ids
    first = next(c for c in chunks if c.id == "account-and-login#1")
    assert first.title == "Account and login > Resetting your password"
    assert "60 minutes" in first.text


def test_every_chunk_has_text():
    assert all(c.text.strip() for c in load_chunks())
