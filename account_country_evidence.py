"""Country evidence shared by channel and account checks, without paid lookups.

The US media hint is the existing channel check's inference, not a claim about
the account owner's residence. Never turn a display's default US into evidence.
"""
from urllib.parse import urlsplit


def valid_country(value):
    value = str(value or '').strip().upper()
    return value if len(value) == 2 and value.isascii() and value.isalpha() and value not in {'UN', 'XX', 'ZZ', 'EU', 'XO'} else ''


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
