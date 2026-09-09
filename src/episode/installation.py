from urllib.parse import urlsplit, urlunsplit

EXTERNAL_EPISODE_URL_SETTING = "external_episode_url"
MAX_EXTERNAL_EPISODE_URL_LENGTH = 2048


def normalize_external_episode_url(url: str) -> str:
    """Validate and normalize the operator-visible address of this installation."""

    if not isinstance(url, str):
        raise ValueError("external Episode URL must be a string")
    value = url.strip()
    if not value:
        return ""
    if len(value) > MAX_EXTERNAL_EPISODE_URL_LENGTH:
        raise ValueError("external Episode URL must not exceed 2048 characters")
    if any(character.isspace() or ord(character) == 0x7F for character in value):
        raise ValueError("external Episode URL must not contain whitespace or control characters")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError as error:
        raise ValueError("external Episode URL is invalid") from error
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("external Episode URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("external Episode URL must not contain user information")
    if parsed.query or parsed.fragment or "?" in value or "#" in value:
        raise ValueError("external Episode URL must not contain a query or fragment")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


async def get_external_episode_url(repository) -> str:
    value = await repository.get_system_setting(EXTERNAL_EPISODE_URL_SETTING)
    try:
        return normalize_external_episode_url(value or "")
    except ValueError:
        # Invalid operator-managed state must not break notifications or System.
        return ""


async def set_external_episode_url(repository, url: str) -> str:
    value = normalize_external_episode_url(url)
    await repository.set_system_setting(EXTERNAL_EPISODE_URL_SETTING, value)
    return value
