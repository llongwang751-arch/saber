"""Small, explicit fact keys; corrections precede embedding deduplication."""
import hashlib
import re

ALIASES = {"名字": "姓名", "name": "姓名", "称呼": "姓名", "城市": "居住地", "住所": "居住地", "所在地": "居住地"}


def fact_key(key, value):
    key = ALIASES.get(key.strip(), key.strip())
    # Likes are set-valued; a new hobby must not erase unrelated hobbies.
    if key in {"喜好", "忌口与偏好限制"}:
        target = re.sub(r"^(?:避免/不喜欢:\s*|喜欢|不喜欢|讨厌|吃|喝)+", "", value.strip())
        key = "preference:" + target
    return "factkey:" + hashlib.sha256(key.encode()).hexdigest()


def explicit_slots(message):
    from .slot_extractor import SlotExtractor
    # Quoted examples and third-person biographies are not self-disclosure.
    clean = re.sub(r'“[^”]*”|「[^」]*」|"[^"\n]*"|`[^`]*`', "", message)
    if not re.match(r"^\s*(?:我|请用|回答|尽量用|默认用|代码|不要|别给我|请勿|千万别)", clean):
        return []
    return SlotExtractor.extract_slots(clean)
