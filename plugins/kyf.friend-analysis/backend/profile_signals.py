"""Deterministic keyword counts used by 知己分析's chat statistics.

Modified by Know Your Friends, 2026-10-13 (Apache-2.0): the retired portrait
signal helpers were removed; only the keyword counter remains.
"""
from __future__ import annotations

import re
from collections import Counter

STOPWORDS = {"这个", "那个", "我们", "你们", "他们", "自己", "就是", "可以", "一下", "然后", "不是", "没有", "什么", "怎么", "真的", "还是", "还有"}


def keyword_counts(texts):
    try:
        import jieba
    except ImportError:
        jieba = None
    counts = Counter()
    for text in texts:
        tokens = jieba.lcut(text) if jieba else re.findall(r"[一-鿿]+|[A-Za-z]{3,}", text)
        for token in tokens:
            word = token.strip().lower()
            if word in STOPWORDS or (not re.fullmatch(r"[一-鿿]{2,8}|[a-z]{3,24}", word)):
                continue
            counts[word] += 1
    return counts


def keywords_from_counts(counts):
    return [{"word": word, "count": count} for word, count in
            sorted(counts.items(), key=lambda item: (-item[1], item[0])) if count >= 2][:6]


def keywords_from_texts(texts):
    return keywords_from_counts(keyword_counts(texts))
