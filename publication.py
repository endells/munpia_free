"""Book metadata, local illustrations and reflowable EPUB rendering."""
import hashlib
import html
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import tempfile
import warnings
import xml.etree.ElementTree as ET
import zipfile


def clean(value):
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]', '', str(value or ''))


def heading(number, title):
    number = int(number or 0)
    title = ' '.join(clean(title).split())
    # Keep an existing matching prefix instead of producing '34. 34. 유령선'.
    title = re.sub(r'^0*%d\s*(?:[.．]\s*|화\s*[:.\-]?\s*)(?=\S)' % number, '', title)
    return '%d. %s' % (number, title)


def safe_path(folder, relative):
    root = Path(folder).resolve()
    target = root / relative
    target.resolve().relative_to(root)
    return target


def digest(data):
    return hashlib.sha256(data).hexdigest()


def decode_image(data, cover=False):
    """Validate real image bytes; cover is always JPEG, not a renamed PNG/WebP."""
    if not data or len(data) > 20 * 1024 * 1024:
        raise ValueError('이미지가 비어 있거나 20MB를 초과했습니다.')
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise ValueError('이미지 처리에 Pillow가 필요합니다. FF 설치 환경을 확인하세요.') from None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as img:
                fmt = img.format
                if img.width * img.height > 40000000 or fmt not in ('JPEG', 'PNG', 'GIF', 'WEBP'):
                    raise ValueError('지원하지 않거나 해상도가 너무 큰 이미지입니다.')
                img.verify()
            if not cover and fmt in ('JPEG', 'PNG', 'GIF'):
                return data, {'JPEG': 'jpg', 'PNG': 'png', 'GIF': 'gif'}[fmt]
            with Image.open(io.BytesIO(data)) as img:
                img = ImageOps.exif_transpose(img)
                output = io.BytesIO()
                if cover:
                    rgba = img.convert('RGBA')
                    rgb = Image.new('RGB', rgba.size, 'white')
                    rgb.paste(rgba, mask=rgba.getchannel('A'))
                    rgb.save(output, format='JPEG', quality=92)
                    return output.getvalue(), 'jpg'
                img.convert('RGBA').save(output, format='PNG')
                return output.getvalue(), 'png'
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError('이미지를 읽을 수 없거나 해상도가 너무 큽니다.') from None


class BodyParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.images, self.hidden = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if self.hidden:
            return
        if tag in ('br', 'p', 'div', 'li', 'h1', 'h2', 'h3'):
            self.parts.append('\n')
        if tag == 'img':
            attrs = dict(attrs)
            idx = len(self.images)
            self.images.append({'url': attrs.get('src', ''), 'alt': clean(attrs.get('alt') or '삽화')})
            self.parts.append('\n\x00IMAGE%d\x00\n' % idx)

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in ('p', 'div', 'li', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def body_blocks(entry, include_comment=False):
    content = clean(entry.get('content')).replace('\r\n', '\n').replace('\r', '\n')
    attachments = entry.get('attachments') or []
    def picture(match):
        key = match.group(1).strip()
        found = next((a for a in attachments if str(a.get('id', '')) == key), None)
        if found is None:
            found = next((a for a in attachments if key in str(a.get('id', ''))), None)
        url, alt = (found.get('imageUrl', ''), found.get('alt') or '삽화') if found else ('', '삽화')
        return '<img src="%s" alt="%s" />' % (html.escape(str(url), quote=True), html.escape(clean(alt), quote=True))
    # Protect literal angle brackets in plain novel text before inserting image tags.
    is_html = re.search(r'</?(?:p|div|br|span|img|script|style|b|i|strong|em|h[1-6])(?:\s|/?>)', content, re.I)
    if not is_html:
        content = html.escape(html.unescape(content))
    content = re.sub(r'\{@PIC:([^}]+)\}', picture, content)
    parser = BodyParser()
    parser.feed(content)
    parser.close()
    blocks = []
    for part in re.split(r'(\x00IMAGE\d+\x00)', ''.join(parser.parts)):
        marker = re.fullmatch(r'\x00IMAGE(\d+)\x00', part)
        if marker:
            blocks.append(dict(type='image', **parser.images[int(marker.group(1))]))
        elif part.strip():
            blocks.append({'type': 'text', 'text': re.sub(r'\n{4,}', '\n\n\n', part).strip()})
    if include_comment and entry.get('authorComment'):
        blocks.append({'type': 'text', 'text': '[작가의 말]'})
        blocks.extend(body_blocks({'content': entry['authorComment'], 'attachments': attachments}))
    return blocks


def cache_path(folder, eid):
    if not str(eid).isdigit():
        raise ValueError('회차 번호가 잘못되었습니다.')
    return safe_path(folder, '.munpia_free/entries/%s.json' % eid)


def load_cache(folder, row):
    try:
        data = json.loads(cache_path(folder, row['entry_id']).read_text(encoding='utf-8'))
        if data.get('version') == 1 and data.get('txt_sha256') == row['sha256']:
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def valid_image(folder, block):
    try:
        path = safe_path(folder, block['src'])
        return path.is_file() and digest(path.read_bytes()) == block['sha256']
    except (KeyError, OSError, ValueError):
        return False


def media_complete(folder, row):
    cache = load_cache(folder, row)
    return bool(cache and all(b.get('src') and valid_image(folder, b) for b in cache['blocks'] if b['type'] == 'image'))


def save_episode(folder, item, entry, include_comment, image_fetch, write, check, warn, previous=None):
    blocks = body_blocks(entry, include_comment)
    old = {b.get('url'): b for b in (previous or {}).get('blocks', []) if b['type'] == 'image'}
    for index, block in enumerate(blocks):
        check()
        if block['type'] != 'image':
            continue
        prior = old.get(block['url'])
        if prior and valid_image(folder, prior):
            block.update(src=prior['src'], sha256=prior['sha256'])
            continue
        if not block['url']:
            warn('회차 %s: 삽화 주소를 찾지 못했습니다.' % item['id'])
            continue
        try:
            data, ext = decode_image(image_fetch(block['url']))
            relative = 'images/%s_%03d.%s' % (item['id'], index + 1, ext)
            write(safe_path(folder, relative), data)
            block.update(src=relative, sha256=digest(data))
        except (ValueError, OSError) as exc:
            warn('회차 %s: 삽화 저장 실패 (%s)' % (item['id'], str(exc)))
    title = heading(item.get('num'), item.get('title'))
    text_parts = [b['text'] if b['type'] == 'text' else '[삽화: %s]' % b.get('src', '받지 못함') for b in blocks]
    data = (title + '\n\n' + '\n\n'.join(text_parts).strip() + '\n').encode('utf-8')
    cache = {'version': 1, 'title': title, 'txt_sha256': digest(data), 'blocks': blocks,
             'source': {k: entry.get(k) for k in ('content', 'attachments', 'authorComment')}}
    # Cache first; a canceled or failed TXT write cannot match this cache's hash.
    write(cache_path(folder, item['id']), json.dumps(cache, ensure_ascii=False).encode('utf-8'))
    return data


def write_info(folder, novel, write):
    root = ET.Element('ComicInfo')
    fields = {'Title': novel['title'], 'Series': novel['title'], 'Writer': novel.get('authorName'),
              'Penciller': novel.get('illustratorName'), 'Summary': novel.get('introduction'),
              'Genre': ', '.join(map(str, novel.get('genres') or [])),
              'Tags': ', '.join(map(str, novel.get('tags') or [])), 'Publisher': '문피아',
              'LanguageISO': 'ko', 'Web': 'https://m.munpia.com/novel/detail/%s' % novel['id'],
              'Count': novel.get('chapterCount'), 'Format': '웹소설'}
    date = str(novel.get('createdAt') or '')[:10].split('-')
    if len(date) == 3 and all(x.isdigit() for x in date):
        fields.update(Year=int(date[0]), Month=int(date[1]), Day=int(date[2]))
    for key, value in fields.items():
        if value is not None and str(value):
            ET.SubElement(root, key).text = clean(value)
    write(safe_path(folder, 'info.xml'), ET.tostring(root, encoding='utf-8', xml_declaration=True))


def stylesheet(line_height=1.8, paragraph_gap=0.55):
    line_height, paragraph_gap = float(line_height), float(paragraph_gap)
    if not 1.2 <= line_height <= 2.5 or not 0 <= paragraph_gap <= 1.5:
        raise ValueError('EPUB 줄간격·문단 간격 설정이 범위를 벗어났습니다.')
    return '''@charset "UTF-8";
body { margin: 0 4%%; font-family: serif; font-size: 1em; line-height: %.2f; }
p { margin: 0 0 %.2fem; padding: 0; text-indent: 0; text-align: left;
    word-break: normal; overflow-wrap: break-word; widows: 2; orphans: 2; }
p.scene-gap { margin-top: 1.2em; }
h1, h2 { font-size: 1.35em; line-height: 1.45; margin: 1.4em 0 1.5em;
         text-align: left; page-break-after: avoid; }
.illustration { margin: 1em 0; text-align: center; page-break-inside: avoid; }
img { max-width: 100%%; height: auto; }
.cover { margin: 0; padding: 0; text-align: center; }
.cover img { max-width: 100%%; max-height: 98vh; object-fit: contain; }
.missing-image { font-size: .9em; font-style: italic; }
''' % (line_height, paragraph_gap)


def text_xhtml(text):
    # One paragraph per nonempty source line; preserve scene breaks without stacking empty <p>s.
    result, blanks = [], 0
    for line in clean(text).splitlines():
        if not line.strip():
            blanks += 1
            continue
        cls = ' class="scene-gap"' if blanks >= 2 and result else ''
        result.append('<p%s>%s</p>' % (cls, html.escape(line.strip())))
        blanks = 0
    return ''.join(result)


def xhtml(title, body, body_class=''):
    return '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ko"><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"/><title>%s</title><link rel="stylesheet" type="text/css" href="style.css"/></head><body class="%s">%s</body></html>' % (html.escape(clean(title)), body_class, body)


def build_epub(folder, novel, records, output_name, check, warn, line_height=1.8, paragraph_gap=0.55):
    if not records:
        return None
    folder = Path(folder)
    out = safe_path(folder, output_name)
    fd, temp = tempfile.mkstemp(prefix='.epub-', dir=str(folder))
    os.close(fd)
    try:
        with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
            z.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            z.writestr('OEBPS/style.css', stylesheet(line_height, paragraph_gap))
            manifest = [('ncx', 'toc.ncx', 'application/x-dtbncx+xml'), ('style', 'style.css', 'text/css')]
            spine, toc, images = [], [], {}
            cover = safe_path(folder, 'cover.jpg')
            has_cover = cover.is_file()
            if has_cover:
                z.writestr('OEBPS/images/cover.jpg', cover.read_bytes())
                z.writestr('OEBPS/cover.xhtml', xhtml(novel['title'], '<div><img src="images/cover.jpg" alt="표지"/></div>', 'cover'))
                manifest += [('cover-image', 'images/cover.jpg', 'image/jpeg'), ('cover-page', 'cover.xhtml', 'application/xhtml+xml')]
                spine.append('cover-page')
                toc.append(('cover-page', '표지', 'cover.xhtml'))
            for i, row in enumerate(sorted(records, key=lambda r: (r['seq'], r['entry_id']))):
                check()
                cid = 'ch%d' % i
                title = heading(row['seq'], row['episode_title'])
                raw = Path(row['path']).read_bytes()
                if digest(raw) != row['sha256']:
                    raise ValueError('EPUB 생성 중 TXT가 변경되었습니다. 다시 실행하세요.')
                cache = load_cache(Path(row['path']).parent, row)
                if cache:
                    blocks = cache['blocks']
                else:
                    text = raw.decode('utf-8')
                    if text.startswith(title + '\n'):
                        text = text[len(title):].lstrip('\n')
                    blocks = [{'type': 'text', 'text': text}]
                body = []
                for block in blocks:
                    if block['type'] == 'text':
                        body.append(text_xhtml(block['text']))
                    elif valid_image(Path(row['path']).parent, block):
                        ext = Path(block['src']).suffix.lower().lstrip('.')
                        mime = {'jpg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif'}.get(ext)
                        if not mime:
                            raise ValueError('EPUB 삽화 형식을 확인할 수 없습니다.')
                        key = block['sha256']
                        if key not in images:
                            aid, target = 'image%d' % len(images), 'images/%s.%s' % (key, ext)
                            z.writestr('OEBPS/' + target, safe_path(Path(row['path']).parent, block['src']).read_bytes())
                            manifest.append((aid, target, mime))
                            images[key] = target
                        body.append('<div class="illustration"><img src="%s" alt="%s"/></div>' % (images[key], html.escape(clean(block.get('alt') or '삽화'), quote=True)))
                    else:
                        body.append('<p class="missing-image">[삽화를 받지 못했습니다]</p>')
                        warn('회차 %s: EPUB에 넣지 못한 삽화가 있습니다. 기존 파일 보완을 실행하세요.' % row['entry_id'])
                z.writestr('OEBPS/' + cid + '.xhtml', xhtml(title, '<h2>%s</h2>%s' % (html.escape(title), ''.join(body))))
                manifest.append((cid, cid + '.xhtml', 'application/xhtml+xml'))
                spine.append(cid)
                toc.append((cid, title, cid + '.xhtml'))
            uid = 'urn:munpia:novel:' + str(novel['id'])
            opf = ET.Element('package', {'xmlns': 'http://www.idpf.org/2007/opf', 'version': '2.0', 'unique-identifier': 'BookId'})
            meta = ET.SubElement(opf, 'metadata', {'xmlns:dc': 'http://purl.org/dc/elements/1.1/'})
            for tag, value in [('title', novel['title']), ('creator', novel.get('authorName')), ('language', 'ko'), ('publisher', '문피아'), ('description', novel.get('introduction'))]:
                ET.SubElement(meta, 'dc:' + tag).text = clean(value)
            ET.SubElement(meta, 'dc:identifier', {'id': 'BookId'}).text = uid
            if has_cover:
                ET.SubElement(meta, 'meta', {'name': 'cover', 'content': 'cover-image'})
            mf = ET.SubElement(opf, 'manifest')
            for mid, href, mime in manifest:
                ET.SubElement(mf, 'item', {'id': mid, 'href': href, 'media-type': mime})
            sp = ET.SubElement(opf, 'spine', {'toc': 'ncx'})
            for sid in spine:
                ET.SubElement(sp, 'itemref', {'idref': sid})
            if has_cover:
                guide = ET.SubElement(opf, 'guide')
                ET.SubElement(guide, 'reference', {'type': 'cover', 'title': '표지', 'href': 'cover.xhtml'})
            z.writestr('OEBPS/content.opf', ET.tostring(opf, encoding='utf-8', xml_declaration=True))
            ncx = ET.Element('ncx', {'xmlns': 'http://www.daisy.org/z3986/2005/ncx/', 'version': '2005-1'})
            head = ET.SubElement(ncx, 'head')
            for name, value in [('dtb:uid', uid), ('dtb:depth', '1'), ('dtb:totalPageCount', '0'), ('dtb:maxPageNumber', '0')]:
                ET.SubElement(head, 'meta', {'name': name, 'content': value})
            ET.SubElement(ET.SubElement(ncx, 'docTitle'), 'text').text = clean(novel['title'])
            nav = ET.SubElement(ncx, 'navMap')
            for i, (cid, title, href) in enumerate(toc):
                point = ET.SubElement(nav, 'navPoint', {'id': cid, 'playOrder': str(i + 1)})
                ET.SubElement(ET.SubElement(point, 'navLabel'), 'text').text = clean(title)
                ET.SubElement(point, 'content', {'src': href})
            z.writestr('OEBPS/toc.ncx', ET.tostring(ncx, encoding='utf-8', xml_declaration=True))
        check()
        os.replace(temp, str(out))
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return str(out)
