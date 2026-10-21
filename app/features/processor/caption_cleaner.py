import re

LINK_TYPES = {"text_link", "url", "mention", "text_mention"}


def _etype(entity) -> str:
    t = getattr(entity, "type", "")
    return str(getattr(t, "name", t)).lower()


class CaptionCleaner:
    @staticmethod
    def clean(text: str, entities=None) -> str:
        if not text or not text.strip():
            return ""
        clean_text = text
        if entities:
            # آفست‌های تلگرام بر پایه‌ی UTF-16 هستند، نه کاراکتر پایتون
            raw = bytearray(text.encode("utf-16-le"))
            targets = [e for e in entities if _etype(e) in LINK_TYPES]
            for entity in sorted(targets, key=lambda e: e.offset, reverse=True):
                start, end = entity.offset * 2, (entity.offset + entity.length) * 2
                if 0 <= start <= end <= len(raw):
                    raw[start:end] = " ".encode("utf-16-le")
            clean_text = raw.decode("utf-16-le", errors="ignore")
        clean_text = re.sub(r'@\w+', '', clean_text)
        clean_text = re.sub(r'https?://\S+', '', clean_text)
        clean_text = re.sub(r'(?:t|telegram)\.me/\S+', '', clean_text)
        clean_text = re.sub(r'\n{3,}', '\n\n', clean_text)
        clean_text = re.sub(r'[ \t]+\n', '\n', clean_text)
        clean_text = re.sub(r' {2,}', ' ', clean_text)
        clean_text = clean_text.strip()
        # حد مجاز کپشن تلگرام ۱۰۲۴ کاراکتر است
        if len(clean_text) > 1024:
            clean_text = clean_text[:1021].rstrip() + "..."
        return clean_text


caption_cleaner = CaptionCleaner()
