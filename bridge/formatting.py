import html


def format_for_tg(source: str, name: str, text: str) -> str:
    """Заголовок + текст для отправки в Telegram (HTML)."""
    safe_name = html.escape(name)
    header = f"<b>[{source}] {safe_name}:</b>"
    if text:
        return f"{header}\n{html.escape(text)}"
    return header


def format_for_vk(source: str, name: str, text: str) -> str:
    """Заголовок + текст для отправки в VK (plain text)."""
    header = f"[{source}] {name}:"
    if text:
        return f"{header}\n{text}"
    return header
