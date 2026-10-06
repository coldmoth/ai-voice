from ai_voice.transcript import Committer


def final(i, text):
    return {"type": "final", "utterance_id": i, "text": text}


def make():
    t = [0.0]
    return Committer(clock=lambda: t[0]), t


def test_single_da_passes():
    c, _ = make()
    assert c.accept(final("a", "Да.")) == "Да."


def test_repeat_within_window_filtered():
    c, t = make()
    assert c.accept(final("a", "да")) == "да"
    t[0] = 1.0
    assert c.accept(final("b", "Да")) is None


def test_repeat_after_window_passes():
    c, t = make()
    c.accept(final("a", "да"))
    t[0] = 5.0
    assert c.accept(final("b", "Да")) == "Да"


def test_phrase_with_da_never_filtered():
    c, t = make()
    assert c.accept(final("a", "да, конечно")) == "да, конечно"
    t[0] = 0.5
    assert c.accept(final("b", "да, конечно")) == "да, конечно"


def test_normal_text_with_different_ids_passes():
    c, _ = make()
    assert c.accept(final("a", "привет")) == "привет"
    assert c.accept(final("b", "привет")) == "привет"
    assert c.accept(final("b", "другое")) is None
