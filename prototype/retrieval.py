import collections
import math
import re


def terms(text):
    # English identifiers + Chinese characters/bigrams; lightweight prototype tokenizer.
    result = re.findall(r"[a-z0-9_./-]+", text.lower())
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        result.extend(run)
        result.extend(run[i:i+2] for i in range(len(run)-1))
    return result


def bm25(query, rows, limit=30):
    if not rows:
        return []
    docs = [collections.Counter(terms(row['text'])) for row in rows]
    lengths = [sum(doc.values()) for doc in docs]
    avg = sum(lengths) / len(rows) or 1
    df = collections.Counter(word for doc in docs for word in doc)
    scores = []
    for row, doc, length in zip(rows, docs, lengths):
        score = 0.0
        for word in set(terms(query)):
            freq = doc[word]
            if freq:
                idf = math.log(1 + (len(rows)-df[word]+0.5)/(df[word]+0.5))
                score += idf*freq*2.2/(freq+1.2*(0.25+0.75*length/avg))
        if score > 0:
            scores.append((row['chunk_id'], score))
    return sorted(scores, key=lambda x: x[1], reverse=True)[:limit]


def rrf(channels, limit=8):
    scores = collections.defaultdict(float)
    trace = collections.defaultdict(list)
    for channel, ranks in channels.items():
        for rank, chunk_id in enumerate(ranks, 1):
            scores[chunk_id] += 1/(60+rank)
            trace[chunk_id].append(channel)
    return [(cid, score, trace[cid]) for cid, score in
            sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]]
