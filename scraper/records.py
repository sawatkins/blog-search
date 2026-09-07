"""Small shared rules for stored article metadata, independent of downloading."""

import hashlib
import re
from urllib.parse import urlsplit

import regex

from scraper.fetching import normalize_url


_GRAPHEME = regex.compile(r'\X')
_EMOJI_START = regex.compile(r'[\p{Extended_Pictographic}\p{Regional_Indicator}]')

# Exact, manually inspected non-article bodies from the 2026-09-07 index review.
# A post discussing cookies or quoting a disclaimer does not match the whole
# body hash, and must not be rejected merely for containing those words.
NON_ARTICLE_HASHES = {
    '67a2400de450e8b6f1543f1e2d4facb79751f1d9e374646ea35fd459a4ff2973': 'site_furniture',
    'c8047b2c1e83faae6010d75e3bdf360a90fe9a0d68a57a531b586f92a25b8c86': 'cookie_notice',
    '04398737dc476794e0d32bcb60ec8ba43c2668c1a81594be1f174f8bc1f7db7f': 'site_furniture',
    'dab3cb05c622703a2b6ae85577a1764161742b8619e9ec845c2057016d8e48c8': 'site_disclaimer',
    '25c40351806453cea5bb0b780b58758d1855da1a11afdde252a4b85a973d4c8f': 'cookie_notice',
    '5c26aaed7e7ac1fd2436adc556b99cd7e0e25a134cb33a13fad392e717689c5a': 'browser_challenge',
    '77516c6bdfe976c7d1db2f989a9c05ddca46061656d75f886fbea2440cd44abf': 'site_furniture',  # www.sindark.com
    '7129b40f2657bdcabc7d046e49ad21420931d8b346aa682b789f6e43c59daac0': 'site_furniture',  # meteuphoric.com
    '4ac61f95e86b28f92eb01812d2ba1ae66b835a2f1545897d48dc8894b91e1b7b': 'site_furniture',  # gurneyjourney.blogspot.com
    '37e7d98b392176f710029166077dfda7e00a546e7cc11e841a1d44ce595e93fb': 'subscriber_notice',  # www.aiweirdness.com
    '1aa5f6ec1f0c08683e960e04117dfd8469496f112940627c0f1ee9223c9fe87d': 'site_furniture',  # allanberger.com
    '2e95930ee5b6f42356cef2d633651f7f7b55bb448f69de7e1e12d1ee14da68d4': 'site_furniture',  # freek.dev
    '3772b775b2e856229cd1f6662bb2a5195f4f481298fe1cb5c747f123393624f5': 'site_furniture',  # www.rdsaunders.co.uk
    '14b5e9971af13444f98b6a6f9b644f0471cdbd6ec03c81eddbc545e66959a737': 'site_furniture',  # audiocookbook.org
    '5691a5851fdd25f07be80dc3d03fae34cb5cf387565dd3dff8cf2297471e1d77': 'site_furniture',  # wildcornerz.blogspot.com
    '6ba3877f0c64c261c9033b1f5d2cc9f2fd146096c3e78714c22462c551adbf23': 'site_disclaimer',  # sjhoward.co.uk
    'd51ba4bc54f44de8d192d7c70ed929ad90306e2bf2777573b925ba2ed385386f': 'site_furniture',  # scripting.com
    '7f401805539d231e3ae5b5e8a7f0415ec6ffb443d71f726da61da5a4b5b6c547': 'site_furniture',  # quantoisseur.com
    'a66c64101dbdbf3f07db6c7ec665efda041a96a70ff728a20db724d212c58e05': 'site_furniture',  # ajft.org
    '753ec22db5756db7763e9a3ceeac2144810e21dc9d936f009331b6ad81876265': 'cookie_notice',  # svencharleer.com
    '91a1eb0647868bdae44899f8358ce9ea4b64ab56240bcda2de93c91f8c31b4f0': 'site_furniture',  # likepunkneverhappened.blogspot.com
    '7c169e8cae3202742c695c2aba57454037d00a11def15083eb03c9edcc37eff6': 'site_furniture',  # teachertomsblog.blogspot.com
    '0c294408ecb7dfc93efb49f51e4fa8a921aa46907d880b1d1cba4e8ffe66e15d': 'placeholder_text',  # markdalessandro.com
    '35a0229ff667a62823bf51490fa02f77aac1fe7846cb0c6687e24f568ea64098': 'placeholder_text',  # brainbreakthroughcoach.com
    '211349eaf31d942f63f62301cf8ce821f63dbf3653afc0675e6a91c59696df0f': 'site_furniture',  # paxsims.wordpress.com
    'a4cf99ac819cfbc3727f3a1bec02a0154e4ecc33613c10c9726c657e52d6660e': 'site_furniture',  # paxsims.wordpress.com
    '3f523b18aef1c579ab4398e0172fa8ad048cd34060756b7c030940ae6468bee8': 'site_furniture',  # heronsperch.blogspot.com
    'd913a3a82716a2377df95c090845e1d54ffe040ebb1d8cc51cb34ee4c3013633': 'placeholder_text',  # beable2.com
    '3f5fc98224ff0b126bf141012f1bbd49baf9bd28b7f6faf8abcb56a6ad08c653': 'site_furniture',  # oilf.blogspot.com
    '0d9582dd3c648ddf2dd6f09d5e4a0786021f0649593c1ad1dc8818e064c010d7': 'site_furniture',  # elizabethhummel.com
    'a0a943ef0e5ead3c60748904d8dfd43fc9dc31306947f3201a1bef350158384c': 'site_furniture',  # mleddy.blogspot.com
    'dc2bb6bcb5e05734fdbdcb3743edd831829e30c73afbcae740ce3d547e45b50e': 'site_furniture',  # blog.dustinkirkland.com
    '368ca52a1ddef38828b6de05f5b2383847991eed4ac3839ffda4cbe61eeffd12': 'placeholder_text',  # penberg.org
    '6516fd2f49c7bfdc3ae770e3bb03d56ba077e0df2843cf1f45545ff6c9a016fd': 'site_furniture',  # b-ark.ca
    '36184145b16a8d56b9d99fc7f95f989f05e6b303039b5e34d20cd49ce3f0102d': 'site_furniture',  # imjustwalkin.com
    '91569ef4b20bdcf8b7c6afc7103810380b6090b51bcf2099769f10483b07d076': 'site_furniture',  # ginevrakirkland.blog
    '615dd67bce1ff1d339015430453ace27a9a559f2d6f77c26f2af93a8dce73597': 'site_furniture',  # paxsims.wordpress.com
    '84b23c2639be2de6e27b393ba3713f067a07bf598e4c76bd523bdcd50436a429': 'site_furniture',  # www.mattselznick.com
    '8a79a815f08e70ca526b4ef292998e0f05dea3958bb53f73319deb3b1bad729e': 'placeholder_text',  # brainbreakthroughcoach.com
    '341a36bcbefe5b3ebbf27458cc6721f3cab13eb434c2de22a157084964a36cea': 'placeholder_text',  # brainbreakthroughcoach.com
    '0540a58a33c66960d2aa5f9924880145b84b84c24349a841b2a243710d357dc6': 'site_furniture',  # markcarrigan.net
    '55658a29943bb382cf12793810b9223b1e05bd51ba649416d1d58c761cd13014': 'site_furniture',  # ajft.org
    '7068a5797bb6d9ede22cf6df7ab8aba6920ff23d00255129c62ce3ec68ad1d54': 'site_furniture',  # beetleypete.com
    '8f880abb623731062e372a385e3e5f00671da33f7ec9172d24575f810de1ae68': 'site_furniture',  # paxsims.wordpress.com
    '12367ce0eecaaff34b3a263940c1fe1425ceb017bd4f5412e9c22a0a5d5d5f00': 'site_furniture',  # paxsims.wordpress.com
    'c0f89065a4083cc85b280afbb84849384e4fff09fb9c69b27fa7bf912c937238': 'site_furniture',  # scripting.com
}


def clean_title(title, url):
    """Remove leading emoji clusters, not accents, numbers or emoji in the title."""
    title = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', title or '')
    title = ' '.join(title.split()).lstrip('\ufeff\u200b ')
    while title:
        cluster = _GRAPHEME.match(title).group()
        emoji = bool(_EMOJI_START.match(cluster)) or '\u20e3' in cluster
        # Ordinary copyright/trademark notation is text unless explicitly styled
        # as emoji. Respect the Unicode text-presentation selector as well.
        if '\ufe0e' in cluster or (cluster[0] in '©®™' and '\ufe0f' not in cluster):
            emoji = False
        if not emoji:
            break
        title = title[len(cluster):].lstrip()
    return title or urlsplit(url).hostname or 'Untitled post'


def content_fingerprint(text):
    return hashlib.sha256(' '.join(text.split()).encode()).hexdigest()


def nonarticle_reason(text):
    reason = NON_ARTICLE_HASHES.get(content_fingerprint(text))
    if reason:
        return reason
    normalized = ' '.join(text.split()).casefold()
    if normalized.startswith("making sure you're not a bot!") and 'anubis' in normalized:
        return 'browser_challenge'
    return None


def url_variant_key(url):
    """Candidate grouping only; equivalence still requires matching article text."""
    normalized = normalize_url(url)
    if not normalized:
        return None
    parts = urlsplit(normalized)
    # A fragment can name a different post on an annual/single-page blog. HTTP
    # fetching ignores it, but cleanup must not erase that stored distinction.
    fragment = urlsplit(url).fragment
    return (parts.netloc + parts.path.rstrip('/') + ('?' + parts.query if parts.query else '')
            + ('#' + fragment if fragment else ''))
