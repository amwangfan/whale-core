"""Extract content words and verb/object or noun phrases grounded in a single utterance."""
import re

NOUNS = set("跑团 模组 开团时间 角色卡 人物卡 调查员 守秘人 骰子 骰点 线索 理智值 技能检定 理智检定 困难成功 极难成功 大成功 大失败 克苏鲁 世界观 星图 晚餐".split())
VERBS = set("掷骰 投骰 车卡 开团 检定 侦查 侦察 急救 潜行 调查 观测".split())
ABBREVIATIONS = re.compile(r"(?<![a-zA-Z])(?:COC|TRPG|DND|NPC|KP|PL|SAN)(?![a-zA-Z])",re.I)
TITLES = re.compile(r"《([^《》\n]{2,24})》")
STOP = set(("东西 事情 问题 时候 时间 情况 方面 样子 地方 部分 一点 一下 一种 一些 一堆 一句 一句话 玩意 玩意儿 "
            "个人 别人 人家 大家 对方 这边 那边 自己 一会 一次 今天 明天 昨天 现在 之前 之后 以前 以后 结果 原因 "
            "意思 感觉 想法 印象 话说 发现 知道 觉得 感到 以为 认为 看到 看见 听说 可以 能够 需要 可能 应该 "
            "还是 就是 没有 好像 不知道 不觉得 不用 不要 不会 不是 不能 属于 成为 开始 结束 进行 表示 说话 "
            "怎么 什么 为什么 那样 这样 怎么样 这样子 那个 这个 自然 确实 真的 当然 哈哈 笑死 绷不住 卧槽 好耶 "
            "反正 毕竟 总之 懂了 完了 算了 等等 没事 没关系 感谢 谢谢 对不起 没错 意义 类型 可能性 "
            "图片 表情 回复 转发 消息 动画 表情包 网页 链接 文件 聊天记录 "
            "你们 我们 他们 她们 它们 俺们 咱们 自个 那种 这种 哪个 这些 那些 那里 这里 这样 那样 "
            "还有 有没有 只有 出来 进去 进来 出去 起来 下去 过来 过去 起床了 看看 看着 听着 "
            "不想 想要 想到 想想 喜欢 好看 好吃 可惜 晚安 早安 早上好 拜拜 小时 分钟 秒钟 "
            "认识 碰到 看过 看完 看来 看起来 说过 不行 行不行 包括 使用 是不是 感受 属实 "
            "支持 变成 变得 带来 搞得 搞成 应当 拥有 记得 忘了 算是 像是 在于 对于 针对 "
            "不了 得了 我会 我能 我想 我要 你会 你能 你想 你要 他会 他能 她会 她能").split())
NOISE = re.compile(r"https?://\S+|\[[^\]\n]{1,300}\]|[a-f\d]{16,}|@[\w.-]+",re.I)
CONTENT = re.compile(r"[\u3400-\u9fff]{2,12}")
FILLER = re.compile(r"^[哈啊唉哦喔欸诶呃嗯哎呀哇呜嘿嘻嘛捏吧啦草噗乐笑]+$")


def extract(texts):
    try:
        import jieba
        import jieba.posseg as pos
    except ImportError:
        tagger = None
    else:
        tok = jieba.Tokenizer()
        tagger = pos.POSTokenizer(tok)
        for term in NOUNS | VERBS:
            tok.add_word(term,tag="n" if term in NOUNS else "v")
    result = []
    for text in texts:
        body = NOISE.sub(" ",text)
        terms = {s:"作品名" for s in TITLES.findall(body) if s.strip() and s not in STOP and not FILLER.fullmatch(s)}
        terms.update((s.upper(),"专名") for s in ABBREVIATIONS.findall(body))
        if tagger is None:
            # Without a tagger, accept only known content words and explicit titles.
            terms.update((s,"名词" if s in NOUNS else "动词") for s in sorted(NOUNS|VERBS) if s in body)
        else:
            previous = None
            for word,flag in tagger.cut(body):
                kind = "名词" if flag.startswith("n") else "动词" if flag in {"v","vn","vd"} else ""
                if not kind or word in STOP or not CONTENT.fullmatch(word) or FILLER.fullmatch(word):
                    previous = None
                    continue
                terms.setdefault(word,kind)
                if previous and kind=="名词" and len(previous+word)<=10:
                    terms.setdefault(previous+word,"原文短语")
                previous = word
        result.append(terms)
    return ("jieba-pos" if tagger else "domain-terms"), result

if __name__ == "__main__":
    import json
    import sys
    json.dump(extract(json.load(sys.stdin)), sys.stdout)
