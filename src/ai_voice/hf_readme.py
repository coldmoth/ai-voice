import html
import re
from urllib.parse import urljoin, urlsplit

def parse_readme(text, repo, revision):
    text = (text or '')[:65536]
    if text.startswith('---'):
        end = text.find('\n---', 3)
        if end >= 0: text = text[end+4:]
    blocks=[]; images=[]; meaningful=0; lines=text.splitlines(); i=0
    def url(value, image=False):
        value=value.strip().strip('<>')
        if value.startswith('/'):
            value='https://huggingface.co'+value
        elif not value.startswith(('http://','https://')):
            value=urljoin(f'https://huggingface.co/{repo}/resolve/{revision}/' if image else f'https://huggingface.co/{repo}/blob/{revision}/', value)
        p=urlsplit(value)
        return value if p.scheme=='https' and (p.hostname=='huggingface.co' or (p.hostname or '').endswith('.huggingface.co') or p.hostname=='hf.co' or (p.hostname or '').endswith('.hf.co')) else None
    def spans(s):
        s=re.sub(r'<[^>]+>', '', s)
        out=[]; pos=0; pat=re.compile(r'(\*\*|__)(.+?)\1|(?<!\*)\*([^*]+)\*|(?<!_)_([^_]+)_(?!_)|`([^`]+)`|\[([^]]+)\]\(([^)]+)\)')
        for m in pat.finditer(s):
            if m.start()>pos: out.append({'t':html.unescape(s[pos:m.start()])})
            if m.group(2): out.append({'t':m.group(2),'b':True})
            elif m.group(3) or m.group(4): out.append({'t':m.group(3) or m.group(4),'i':True})
            elif m.group(5): out.append({'t':m.group(5),'code':True})
            else:
                v=url(m.group(7)); out.append({'t':m.group(6),**({'href':v} if v else {})})
            pos=m.end()
        if pos<len(s): out.append({'t':html.unescape(s[pos:])})
        return [x for x in out if x.get('t')]
    while i<len(lines) and len(blocks)<300:
        line=lines[i]
        if line.startswith('```'):
            i+=1; buf=[]
            while i<len(lines) and not lines[i].startswith('```'): buf.append(lines[i]); i+=1
            i+=1; blocks.append({'type':'code','text':'\n'.join(buf)}); continue
        m=re.match(r'^#{1,6}\s+(.*)',line)
        if m: blocks.append({'type':'heading','level':min(4,len(line)-len(line.lstrip('#'))),'spans':spans(m.group(1))}); i+=1; continue
        m=re.match(r'^!\[([^]]*)\]\(([^)]+)\)$',line) or re.match(r'^<img\s+[^>]*src=["\']([^"\']+)["\'][^>]*>$',line,re.I)
        if m:
            alt=m.group(1) if line.startswith('!') else '' ; raw=m.group(2) if line.startswith('!') else m.group(1); v=url(raw,True)
            if v and len(images)<6: images.append(v); blocks.append({'type':'image','n':len(images)-1,'alt':alt})
            i+=1; continue
        if re.match(r'^>\s?',line): blocks.append({'type':'quote','spans':spans(re.sub(r'^>\s?','',line))}); meaningful+=len(line); i+=1; continue
        lm=re.match(r'^\s*([-+*]|\d+\.)\s+(.*)',line)
        if lm:
            ordered=lm.group(1)[0].isdigit(); items=[]
            while i<len(lines):
                mm=re.match(r'^\s*([-+*]|\d+\.)\s+(.*)',lines[i])
                if not mm or (mm.group(1)[0].isdigit())!=ordered: break
                items.append(spans(mm.group(2))); meaningful+=len(mm.group(2)); i+=1
            blocks.append({'type':'list','ordered':ordered,'items':items}); continue
        if line.strip():
            buf=[re.sub(r'<[^>]+>','',line)]; i+=1
            while i<len(lines) and lines[i].strip() and not re.match(r'^(#{1,6}\s|```|>|\s*[-+*]\s|\s*\d+\.\s|!\[|<img)',lines[i]): buf.append(re.sub(r'<[^>]+>','',lines[i])); i+=1
            s=' '.join(buf); blocks.append({'type':'paragraph','spans':spans(s)}); meaningful+=len(s)
        else: i+=1
    return {'blocks':blocks,'images':images,'meaningful':meaningful>=40}
