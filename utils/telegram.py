import html
import httpx
import logging

log = logging.getLogger("micro.telegram")


def _escape_question(text: str) -> str:
    """Escape HTML in user-generated content (market questions) while keeping our tags."""
    # Split on our known tags, escape everything else
    import re
    parts = re.split(r'(</?(?:b|i|code|a)(?: [^>]*)?>)', text)
    return "".join(p if re.match(r'</?(?:b|i|code|a)(?: [^>]*)?>$', p) else html.escape(p) for p in parts)


class TelegramBot:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.client = httpx.AsyncClient(timeout=10.0)

    async def send(self, text: str):
        if not self.token or not self.chat_id:
            return
        try:
            r = await self.client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": _escape_question(text), "parse_mode": "HTML"},
            )
            if r.status_code != 200:
                raise ValueError(f"HTTP {r.status_code}: {r.text[:200]}")
            log.debug(f"[TG] Sent OK ({r.status_code})")
        except Exception as e:
            log.warning(f"[TG] HTML send failed: {e}, trying plain text")
            try:
                import re as _re
                plain = _re.sub(r'<a [^>]*>', '', text)
                plain = plain.replace("</a>", "")
                plain = plain.replace("<b>", "").replace("</b>", "")
                plain = plain.replace("<i>", "").replace("</i>", "")
                plain = plain.replace("<code>", "").replace("</code>", "")
                await self.client.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": plain[:4096]},
                )
            except Exception as e2:
                log.error(f"[TG] {e2}")

    async def close(self):
        await self.client.aclose()
