"""Turn a user's website address or search phrase into a browser URL."""
from __future__ import annotations

import re
from urllib.parse import quote_plus, urlsplit, urlunsplit


def web_target(text: str) -> str:
    value = text.strip()
    if not value:
        raise ValueError('Введи адресу сайту або пошуковий запит.')
    if any(ord(char) < 32 for char in value):
        raise ValueError('Адреса містить недопустимі символи.')
    explicit = bool(re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', value))
    if explicit:
        candidate = value
    elif re.match(r'^(?:javascript|data|file|mailto|ftp):', value, re.I):
        raise ValueError('Для вебсайтів використовуй адресу HTTP або HTTPS.')
    elif not any(char.isspace() for char in value) and (
        '.' in value.split('/')[0] or value.startswith(('localhost', '['))
    ):
        candidate = 'https://' + value
    else:
        return 'https://duckduckgo.com/?q=' + quote_plus(value)
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in {'https', 'http'} or not parsed.hostname:
            raise ValueError('Вкажи коректну адресу HTTP або HTTPS.')
        if parsed.username is not None or parsed.password is not None:
            raise ValueError('Введи адресу сайту без логіна й пароля.')
        host = parsed.hostname.encode('idna').decode('ascii')
        if any(char.isspace() for char in host) or '\\' in host:
            raise ValueError('Перевір назву сайту.')
        port = parsed.port
        authority = '[' + host + ']' if ':' in host else host
        if port is not None:
            authority += ':' + str(port)
        return urlunsplit((parsed.scheme.lower(), authority, parsed.path, parsed.query, parsed.fragment))
    except (UnicodeError, ValueError) as error:
        raise ValueError(str(error)) from None
