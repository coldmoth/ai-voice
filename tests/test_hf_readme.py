"""Offline contracts for the restricted HF README renderer."""
from ai_voice.hf_readme import parse_readme


def parse(text):
    return parse_readme(text, 'test/voice', 'abc')


def test_front_matter_and_all_block_types():
    result = parse('---\nlicense: mit\n---\n# Title\n\n## Subtitle\n\n'
                   '- one\n- two\n\n1. first\n2. second\n\n'
                   '```python\nprint(1)\n```\n\n> Quoted text\n\n' + 'Description ' * 5)
    assert [b['type'] for b in result['blocks']] == ['heading', 'heading', 'list', 'list', 'code', 'quote', 'paragraph']
    assert result['blocks'][2]['ordered'] is False
    assert result['blocks'][3]['ordered'] is True
    assert result['blocks'][4]['text'] == 'print(1)'
    assert result['meaningful'] is True
    assert parse('###### Deep')['blocks'][0]['level'] == 4


def test_spans_and_safe_links():
    spans = parse('**bold** __also bold__ *italic* _also italic_ `code` '
                  '[relative](file.md) [bad](javascript:alert) [data](data:text/plain,x)')['blocks'][0]['spans']
    assert {'t': 'bold', 'b': True} in spans
    assert {'t': 'also bold', 'b': True} in spans
    assert {'t': 'italic', 'i': True} in spans
    assert {'t': 'also italic', 'i': True} in spans
    assert {'t': 'code', 'code': True} in spans
    assert {'t': 'relative', 'href': 'https://huggingface.co/test/voice/blob/abc/file.md'} in spans
    assert {'t': 'bad'} in spans and {'t': 'data'} in spans


def test_relative_html_images_and_limit():
    result = parse('![cover](cover.png)\n<img src="https://cdn-uploads.huggingface.co/a.png">\n'
                   '![bad](https://outside.test/img.png)\n' + '\n'.join(f'![{i}](img{i}.png)' for i in range(8)))
    assert len(result['images']) == 6
    assert result['images'][0] == 'https://huggingface.co/test/voice/resolve/abc/cover.png'
    assert result['images'][1] == 'https://cdn-uploads.huggingface.co/a.png'
    assert [b['n'] for b in result['blocks']] == list(range(6))


def test_html_removed_from_headings_lists_quotes_and_paragraphs():
    blocks = parse('# <b>Title</b>\n\n- <i>Item</i>\n\n> <em>Quote</em>\n\n<script>alert(1)</script>')['blocks']
    assert blocks[0]['spans'] == [{'t': 'Title'}]
    assert blocks[1]['items'] == [[{'t': 'Item'}]]
    assert blocks[2]['spans'] == [{'t': 'Quote'}]
    assert blocks[3]['spans'] == [{'t': 'alert(1)'}]


def test_template_and_input_block_limits():
    assert parse('# Template\n\n1234567890')['meaningful'] is False
    assert len(parse('\n\n'.join('Paragraph' for _ in range(500)))['blocks']) == 300
    text = ''.join(s['t'] for s in parse('a' * 70000)['blocks'][0]['spans'])
    assert len(text) == 65536
