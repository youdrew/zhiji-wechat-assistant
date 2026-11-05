"""Validate the small public website closure and its published Windows asset."""
from __future__ import annotations

import argparse
from hashlib import sha256
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / 'site'
DEMO_FILES = {
    'chat': {'index.html', 'demo.css', 'demo.js', 'message-analysis.css', 'advisor-panel.css'},
    'friend': {'index.html', 'styles.css', 'renderer.js', 'demo.js', 'analysis-fixtures.js'},
    'self': {'index.html', 'demo.css', 'demo-host.js', 'demo-data.js', 'page.css', 'page.js',
             'persona-charts.css', 'persona-charts.js'},
    'extensions': {'index.html', 'core-ui.css', 'plugin-settings.css', 'settings-window.css',
                   'plugin-settings.js', 'plugin-configuration.js', 'plugin-fixtures.js',
                   'demo.css', 'demo.js'},
}
DEMO_PAGES = {f'demo/{demo}/index.html' for demo in DEMO_FILES}
OFFICIAL_ICONS = {
    'app-icon.png': '79fa0c600fb087e25e8271a0af5a074f52068231f18ae6cc4f7357e22c983929',
    'favicon.ico': '5cc524c4266f32a0d88ec0e0eb4b367e86c2bb075ff685c9c92cc4822bd49bd2',
}
BRAND_IMAGE = {'class': 'brand-symbol', 'src': 'app-icon.png', 'alt': '',
               'width': '32', 'height': '32', 'aria-hidden': 'true'}
EXPECTED = {'index.html', 'install.html', 'styles.css', 'app.js', 'copy.js', 'release.json'} | OFFICIAL_ICONS.keys() | {
    f'demo/{demo}/{name}' for demo, names in DEMO_FILES.items() for name in names
}


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.references = []
        self.images = []
        self.frames = []
        self.csp = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('http-equiv', '').lower() == 'content-security-policy':
            self.csp = attrs.get('content', '')
        if 'id' in attrs:
            self.ids.append(attrs['id'])
        for field in ('href', 'src'):
            if attrs.get(field):
                self.references.append(attrs[field])
        if tag == 'img' and attrs.get('src'):
            self.images.append(attrs)
        if tag == 'iframe':
            self.frames.append(attrs)


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
    for name, digest in OFFICIAL_ICONS.items():
        assert sha256((SITE / name).read_bytes()).hexdigest() == digest, 'Website icon differs from the official application asset'
    pages = {}
    for name in ('index.html', 'install.html', *sorted(DEMO_PAGES)):
        path = SITE / name
        html = path.read_text(encoding='utf-8')
        is_demo = name in DEMO_PAGES
        if not is_demo:
            assert not re.search(r'\bpro\b', html, re.I), 'Public copy must describe shipped features'
        page = Page()
        page.feed(html)
        pages[name] = page
        assert len(page.ids) == len(set(page.ids)), 'Duplicate page IDs'
        assert page.images == ([] if is_demo else [BRAND_IMAGE]), 'Only the official header icon is allowed; showcases must use interactive demos'
        if is_demo:
            directives = {parts[0]: parts[1:] for entry in page.csp.split(';') if (parts := entry.split())}
            assert directives.get('default-src') == ["'none'"]
            assert directives.get('connect-src', directives.get('default-src')) == ["'none'"]
            assert directives.get('script-src') == ["'self'"]
        if name == 'index.html':
            assert len(page.frames) == len(DEMO_PAGES)
            assert {urllib.parse.urlsplit(frame.get('src', '')).path for frame in page.frames} == DEMO_PAGES
            assert all(frame.get('sandbox') == 'allow-scripts' and frame.get('title')
                       for frame in page.frames), 'Demo frames must be isolated and labeled'
        else:
            assert not page.frames, 'Unexpected nested frame'
        for ref in page.references:
            if name == 'index.html' and ref == 'mailto:youdrewxuan@icloud.com':
                continue
            parsed = urllib.parse.urlsplit(ref)
            if is_demo:
                assert not parsed.scheme and not parsed.netloc, 'Demo references must stay local'
            if parsed.scheme:
                assert parsed.scheme == 'https'
                assert parsed.hostname in ('github.com', 'eugenepage.com', 'developers.openai.com',
                                           'www.codebuddy.cn', 'open.workbuddy.cn')
                assert not parsed.username and not parsed.password
                continue
            assert not parsed.netloc, 'Protocol-relative URLs are not allowed'
            if parsed.path:
                target = (path.parent / urllib.parse.unquote(parsed.path)).resolve()
                assert target.is_relative_to(path.parent.resolve()) and target.is_file(), 'Broken local file link'
            if parsed.fragment and not parsed.path:
                assert parsed.fragment in page.ids, 'Broken local anchor'
    release = validate_release(json.loads((SITE / 'release.json').read_text(encoding='utf-8')), required=args.require_download)
    print(json.dumps({'siteFiles': len(actual), 'interactiveDemos': len(DEMO_PAGES), 'screenshots': 0,
                      'download': release, 'publicAssetVerified': args.require_download}))


if __name__ == '__main__':
    main()
