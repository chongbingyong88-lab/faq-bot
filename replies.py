"""Everything the bot says on its own account, in one place.

Not answers — those come from the model and the clinic's own website. These
are the lines it falls back to: a price it may not quote, a handover, a
voice note it could not hear, its own failure.

They were written one at a time across three files, and it showed. Nearly
every one said "colleague", several apologised for themselves ("Sorry, I hit
a problem answering that"), and the register wandered between them.

The voice they share now:

  · a receptionist, not a chatbot. "our team", never "my colleague".
  · say what happens next, in the same message. Every line ends with the
    customer knowing who is doing what.
  · state a problem once, do not apologise twice for it. A bot that keeps
    saying sorry reads as broken.
  · Malay and Chinese are written, not translated — "pada pukul 9am" inside
    a Malay sentence is what a half-finished product looks like.
"""
from __future__ import annotations

LINES: dict[str, dict[str | None, str]] = {
    # A price exists on the site but may not be quoted: a market rate, a
    # masked figure, a discount with no base price.
    "price_unavailable": {
        None: ("That one depends on your case, so we don't publish a fixed "
               "price. Our team will confirm it for you shortly."),
        "Malay": ("Harga itu bergantung pada keadaan gigi anda, jadi kami "
                  "tidak menyenaraikan harga tetap. Pasukan kami akan "
                  "sahkan untuk anda sebentar lagi."),
        "Chinese": "这项收费需要看您的情况，所以我们没有公开固定价格。我们的团队会尽快为您确认。",
    },
    # The site simply does not answer it.
    "handing_over": {
        None: ("I'll pass this to our team and they'll reply here shortly."),
        "Malay": ("Saya akan serahkan kepada pasukan kami dan mereka akan "
                  "membalas di sini sebentar lagi."),
        "Chinese": "我会把这个交给我们的团队，他们会尽快在这里回复您。",
    },
    # A person has picked it up. Said once per handover.
    "person_has_it": {
        None: ("Someone from our team has your message and will reply here "
               "shortly."),
        "Malay": ("Pasukan kami sudah menerima mesej anda dan akan membalas "
                  "di sini sebentar lagi."),
        "Chinese": "我们的团队已经收到您的信息，稍后会在这里回复您。",
    },
    # Waiting a while with nobody back yet.
    "still_with_us": {
        None: ("Thanks for your patience — our team still has your question "
               "and will reply as soon as they can."),
        "Malay": ("Terima kasih kerana menunggu — pasukan kami masih "
                  "menyemak pertanyaan anda dan akan membalas secepat "
                  "mungkin."),
        "Chinese": "感谢您的耐心等待，我们的团队仍在处理您的问题，会尽快回复您。",
    },
    # Our fault, not theirs. Named once, then what happens next.
    "trouble": {
        None: ("I'm having trouble on my end. Our team will pick this up and "
               "reply here shortly."),
        "Malay": ("Saya menghadapi masalah teknikal. Pasukan kami akan "
                  "mengambil alih dan membalas di sini sebentar lagi."),
        "Chinese": "我这边出现了一些问题。我们的团队会接手并尽快回复您。",
    },
    "voice_unclear": {
        None: ("I couldn't quite catch that voice note — could you type it "
               "instead?"),
        "Malay": ("Saya tidak dapat menangkap nota suara itu dengan jelas — "
                  "boleh anda taipkan?"),
        "Chinese": "我没有听清楚这段语音，可以麻烦您打字吗？",
    },
    "voice_empty": {
        None: ("That voice note came through empty — could you send it again "
               "or type your question?"),
        "Malay": ("Nota suara itu kosong — boleh hantar semula atau taipkan "
                  "soalan anda?"),
        "Chinese": "这段语音是空的，可以麻烦您重新发送或打字吗？",
    },
    "media_unsupported": {
        None: ("I can read text and voice notes. For photos or documents, "
               "our team will take a look and reply here."),
        "Malay": ("Saya boleh membaca teks dan nota suara. Untuk gambar atau "
                  "dokumen, pasukan kami akan menyemak dan membalas di sini."),
        "Chinese": "我可以阅读文字和语音。图片或文件的话，我们的团队会查看并回复您。",
    },
}


def line(key: str, lang: str | None = None) -> str:
    """The line, in the customer's language, falling back to English."""
    variants = LINES[key]
    return variants.get(lang, variants[None])
