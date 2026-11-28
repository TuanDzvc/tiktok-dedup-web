"""Country evidence shared by channel and account checks, without paid lookups.

The US media hint is the existing channel check's inference, not a claim about
the account owner's residence. Never turn a display's default US into evidence.
"""
from urllib.parse import urlsplit
import json
import re


def valid_country(value):
    value = str(value or '').strip().upper()
    return value if len(value) == 2 and value.isascii() and value.isalpha() and value not in {'UN', 'XX', 'ZZ', 'EU', 'XO'} else ''


def public_profile_country(html, username, uid=''):
    """Only the requested user's own fields are evidence, never viewer/CDN routing."""
    match = re.search(r'<script\b(?=[^>]*\bid=["\x27]__UNIVERSAL_DATA_FOR_REHYDRATION__["\x27])[^>]*>([\s\S]*?)</script>', str(html))
    if not match:
        return ''
    try:
        scope = json.loads(match.group(1)).get('__DEFAULT_SCOPE__') or {}
        detail = scope.get('webapp.user-detail') or {}
        user = (detail.get('userInfo') or {}).get('user') or {}
        if detail.get('statusCode', 0) != 0 or str(user.get('uniqueId', '')).lower() != str(username).lstrip('@').lower():
            return ''
        if uid and str(user.get('id') or user.get('uid') or '') != str(uid):
            return ''
        return next((valid_country(user.get(key)) for key in ('region', 'country', 'account_region') if valid_country(user.get(key))), '')
    except (ValueError, TypeError, AttributeError):
        return ''


def borrowed_profile_country(username, uid, pool, *, http, default_ua):
    if not re.fullmatch(r'[A-Za-z0-9._]{1,64}', str(username or '')):
        return ''
    for cookie, ua, _ in (pool or [])[:2]:
        try:
            response = http.get('https://www.tiktok.com/@' + username,
                headers={'User-Agent': ua or default_ua, 'Cookie': cookie, 'Accept-Language': 'en-US,en;q=0.9'},
                timeout=6, allow_redirects=False)
            if response.status_code == 200:
                country = public_profile_country(response.text, username, uid)
                if country:
                    return country
        except Exception:
            continue
    return ''


def honest_history_country(item, cached_country=''):
    """Old empty-channel results defaulted to US without storing evidence."""
    channel = item.get('channel') if isinstance(item.get('channel'), dict) else {}
    if item.get('videos') or item.get('country_source') or channel.get('country_source'):
        return item
    country = valid_country(cached_country)
    display = country or 'Không rõ'
    result = dict(item, region=country, region_display=display)
    if channel:
        result['channel'] = dict(channel, region=country, region_display=display, country_source='shared_cache' if country else '')
    if isinstance(item.get('user_info'), dict):
        result['user_info'] = dict(item['user_info'], country=display)
    return result


def country_evidence(posts, profile_country=''):
    for post in posts or []:
        if not isinstance(post, dict):
            continue
        author = post.get('author') if isinstance(post.get('author'), dict) else {}
        for raw in (post.get('author_region'), author.get('region'), author.get('account_region')):
            code = valid_country(raw)
            if code:
                return code, 'author_profile'
    if valid_country(profile_country):
        return valid_country(profile_country), 'profile'
    for post in posts or []:
        if not isinstance(post, dict):
            continue
        urls = [post.get('cover_url')]
        video = post.get('video') if isinstance(post.get('video'), dict) else {}
        for key in ('cover', 'origin_cover', 'dynamic_cover', 'play_addr'):
            value = video.get(key)
            if isinstance(value, str):
                urls.append(value)
            elif isinstance(value, dict):
                urls.extend(value.get('url_list') or [])
        for url in urls:
            if not isinstance(url, str):
                continue
            try:
                parsed = urlsplit(url)
                host = (parsed.hostname or '').lower()
                official = any(host == domain or host.endswith('.' + domain)
                               for domain in ('tiktokcdn.com', 'tiktokcdn-us.com', 'byteoversea.com', 'ibytedtos.com'))
                if official and 'tos-useast8' in parsed.path.lower():
                    return 'US', 'media_storage_us'
            except ValueError:
                pass
    return '', ''
