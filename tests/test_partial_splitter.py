from ai_voice.transcript import PartialSplitter

LONG = "один два три четыре пять шесть семь восемь девять десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать"


def make():
    t = [0.0]
    return PartialSplitter(12, clock=lambda: t[0]), t


def test_short_phrase_only_final():
    sp, t = make()
    assert sp.partial("u", "привет как дела") == []
    t[0] = 1.0
    assert sp.partial("u", "привет как дела друг") == []
    assert sp.final("u", "Привет, как дела, друг") == "Привет, как дела, друг"


def test_long_stable_prefix_then_remainder():
    sp, t = make()
    words = LONG.split()
    assert sp.partial("u", " ".join(words[:14])) == []
    t[0] = 0.7
    out = sp.partial("u", " ".join(words[:15]))
    assert len(out) == 1
    final = LONG + " шестнадцать"
    rest = sp.final("u", final)
    assert (" ".join(out) + " " + rest).split() == final.split()


def test_unstable_prefix_not_committed():
    sp, t = make()
    sp.partial("u", LONG)
    t[0] = 1.0
    changed = "ноль " + LONG
    assert sp.partial("u", changed) == []


def test_punctuation_boundary():
    sp, t = make()
    text = "Сегодня хорошая погода, и я иду гулять в парк, потом домой уже вечером когда стемнеет совсем"
    sp.partial("u", text)
    t[0] = 0.7
    out = sp.partial("u", text + " и всё")
    assert out == ["Сегодня хорошая погода, и я иду гулять в парк,"]


def test_rewritten_start_no_duplicates():
    sp, t = make()
    sp.partial("u", LONG)
    t[0] = 0.7
    out = sp.partial("u", LONG + " шестнадцать")
    assert out
    rest = sp.final("u", "1 " + LONG + " шестнадцать")
    assert rest.split() == ("1 " + LONG + " шестнадцать").split()[len(out[0].split()):]


def test_empty_remainder():
    sp, t = make()
    sp.partial("u", LONG)
    t[0] = 0.7
    out = sp.partial("u", LONG)
    assert out
    full = " ".join(out)
    assert sp.final("u", full) == ""
