"""Recent anime links shown in the URL selector."""


def remember_url(history, url, limit=10):
    url = str(url or "").strip()
    if not url:
        return list(history or [])[:limit]
    return [url] + [item for item in history or [] if item != url and isinstance(item, str)][:limit - 1]
