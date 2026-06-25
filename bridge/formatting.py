import html


def format_for_tg(source: str, name: str, text: str, prefix: str = "") -> str:
    """Заголовок + текст для отправки в Telegram (HTML).

    prefix — персональный префикс автора (эмодзи), ставится перед именем:
    «[VK] 🕋Ирина Седойкина:».
    """
    safe_name = html.escape(name)
    safe_prefix = html.escape(prefix or "")
    header = f"<b>[{source}] {safe_prefix}{safe_name}:</b>"
    if text:
        return f"{header}\n{html.escape(text)}"
    return header


def format_for_vk(source: str, name: str, text: str, prefix: str = "") -> str:
    """Заголовок + текст для отправки в VK (plain text)."""
    header = f"[{source}] {prefix or ''}{name}:"
    if text:
        return f"{header}\n{text}"
    return header
