"""Validate the small public website closure and its published Windows asset."""
from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import struct
import urllib.parse
import urllib.request
import zlib

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / 'site'
ASSETS = {
    'chat-suggestions.png', 'friend-overview.png', 'relationship-memories.png',
    'chat-assistant.png', 'interaction-trends.png', 'self-timeline.png',
    'self-personality.png', 'model-settings.png', 'plugin-management.png',
}
EXPECTED = {'index.html', 'install.html', 'styles.css', 'app.js', 'copy.js', 'release.json', 'favicon.svg'} | {
    'assets/' + name for name in ASSETS
}


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.references = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.append(attrs['id'])
        for field in ('href', 'src'):
            if attrs.get(field):
                self.references.append(attrs[field])
        if tag == 'img' and attrs.get('src'):
            assert attrs.get('alt'), 'A screenshot is missing its description'
            self.images.append(attrs)


def image_dimensions(path):
    data = path.read_bytes()
    assert data[:8] == b'\x89PNG\r\n\x1a\n', 'Unexpected screenshot format'
    offset = 8
    dimensions = None
    while offset < len(data):
        size = int.from_bytes(data[offset:offset + 4], 'big')
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + size]
        crc = int.from_bytes(data[offset + 8 + size:offset + 12 + size], 'big')
        assert kind in (b'IHDR', b'IDAT', b'IEND'), 'Unexpected screenshot metadata'
        assert zlib.crc32(kind + payload) & 0xffffffff == crc, 'Screenshot CRC differs'
        if kind == b'IHDR':
            dimensions = struct.unpack('>II', payload[:8])
        offset += size + 12
    assert offset == len(data) and dimensions
    return dimensions


def validate_release(value, *, required):
    if value == {'state': 'pending'}:
        assert not required, 'Publish and verify the Windows installer before deploying the website'
        return 'pending'
    assert set(value) == {'state', 'version', 'filename', 'downloadUrl', 'notesUrl', 'sha256', 'sizeBytes', 'signed'}
    assert value['state'] == 'published'
    assert re.fullmatch(r'\d+\.\d+\.\d+', value['version'])
    filename = f"Zhiji-WeChat-Assistant-{value['version']}-Setup-x64.exe"
    prefix = 'https://github.com/youdrew/zhiji-wechat-assistant/releases/'
    assert value['filename'] == filename
    assert value['downloadUrl'] == prefix + f"download/v{value['version']}/{filename}"
    assert value['notesUrl'] == prefix + 'tag/v' + value['version']
    assert re.fullmatch('[a-f0-9]{64}', value['sha256'])
    assert type(value['sizeBytes']) is int and value['sizeBytes'] > 0
    assert type(value['signed']) is bool
    if required:
        route = 'https://api.github.com/repos/youdrew/zhiji-wechat-assistant/releases/tags/v' + value['version']
        headers = {'User-Agent': 'Zhiji-public-website-check', 'Accept': 'application/vnd.github+json'}
        request = urllib.request.Request(route, headers=headers)
        if os.environ.get('GITHUB_TOKEN'):
            request.add_unredirected_header('Authorization', 'Bearer ' + os.environ['GITHUB_TOKEN'])
        with urllib.request.urlopen(request, timeout=45) as response:
            release = json.load(response)
        assert not release['draft'] and not release['prerelease'], 'Installer release is not public and stable'
        assets = [a for a in release['assets'] if a['name'] == filename]
        assert len(assets) == 1
        asset = assets[0]
        assert asset['state'] == 'uploaded'
        assert asset['digest'] == 'sha256:' + value['sha256']
        assert asset['size'] == value['sizeBytes']
        assert asset['browser_download_url'] == value['downloadUrl']
        with urllib.request.urlopen(urllib.request.Request(value['downloadUrl'], method='HEAD', headers={'User-Agent': headers['User-Agent']}), timeout=45) as response:
            assert response.status == 200
            assert int(response.headers['Content-Length']) == value['sizeBytes']
    return value['version']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--require-download', action='store_true')
    args = parser.parse_args()
    actual = set()
    for path in SITE.rglob('*'):
        assert not path.is_symlink(), 'Website assets cannot be linked'
        if path.is_file():
            actual.add(path.relative_to(SITE).as_posix())
    assert actual == EXPECTED, 'Website artifact contains unexpected or missing files'
    pages = {}
    for name in ('index.html', 'install.html'):
        html = (SITE / name).read_text(encoding='utf-8')
        assert not re.search(r'\bpro\b', html, re.I), 'Public copy must describe shipped features'
        page = Page()
        page.feed(html)
        pages[name] = page
        assert len(page.ids) == len(set(page.ids)), 'Duplicate page IDs'
        for ref in page.references:
            if name == 'index.html' and ref == 'mailto:youdrewxuan@icloud.com':
                continue
            parsed = urllib.parse.urlsplit(ref)
            if parsed.scheme:
                assert parsed.scheme == 'https'
                assert parsed.hostname in ('github.com', 'eugenepage.com', 'developers.openai.com',
                                           'www.codebuddy.cn', 'open.workbuddy.cn')
                assert not parsed.username and not parsed.password
                continue
            assert not parsed.netloc, 'Protocol-relative URLs are not allowed'
            if parsed.path:
                target = (SITE / urllib.parse.unquote(parsed.path)).resolve()
                assert target.is_relative_to(SITE.resolve()) and target.is_file(), 'Broken local file link'
            if parsed.fragment and not parsed.path:
                assert parsed.fragment in page.ids, 'Broken local anchor'
    screenshots = pages['index.html'].images
    assert len(screenshots) == len(ASSETS)
    assert {urllib.parse.urlsplit(image['src']).path for image in screenshots} == {'assets/' + name for name in ASSETS}
    assert not pages['install.html'].images
    for image in screenshots:
        path = urllib.parse.urlsplit(image['src']).path
        assert image_dimensions(SITE / path) == (int(image['width']), int(image['height'])), 'Screenshot aspect ratio metadata differs'
    release = validate_release(json.loads((SITE / 'release.json').read_text(encoding='utf-8')), required=args.require_download)
    print(json.dumps({'siteFiles': len(actual), 'screenshots': len(pages['index.html'].images), 'download': release, 'publicAssetVerified': args.require_download}))


if __name__ == '__main__':
    main()
