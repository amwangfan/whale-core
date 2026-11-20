"""A group's curated roster and its locally generated, evidence-linked portraits."""
import json
from pathlib import Path
from tempfile import NamedTemporaryFile


def directory_path(archive, gid):
    return Path(archive).parent / "group_profiles" / gid


def load_directory(archive, gid):
    if not archive or not gid.isdigit():
        return None
    path = directory_path(archive, gid) / "群资料.json"
    if not path.is_file():
        return None
    roster = json.loads(path.read_text(encoding="utf-8-sig"))
    if roster["group_id"] != gid:
        raise ValueError("群资料与群号不一致")
    return roster


def account_names(roster):
    return {account: p["name"] for p in roster["members"] + roster["bots"] for account in p["accounts"]}


def name_hint(roster):
    labels = [p["name"] + ("（又称" + "、".join(p["aliases"]) + "）" if p["aliases"] else "")
              for p in roster["members"]]
    bots = [p["name"] + "（" + "、".join(p["aliases"]) + "）" for p in roster["bots"]]
    return (f"[群资料] {roster['name']}，{roster['description']}。"
            f"{len(labels)} 位成员，称呼使用：{'、'.join(labels)}。工具账号：{'、'.join(bots)}。")


def portrait_path(archive, gid, member):
    name = member["name"]
    if not name or Path(name).name != name or name in (".", ".."):
        raise ValueError("成员文件名无效")
    return directory_path(archive, gid) / "members" / (name + ".json")


def read_portrait(archive, gid, member):
    path = portrait_path(archive, gid, member)
    if not path.is_file():
        return None
    profile = json.loads(path.read_text(encoding="utf-8"))
    if (profile["group_id"], profile["accounts"]) != (gid, member["accounts"]):
        return None
    return profile


def roster_members(index, roster):
    indexed = {p["id"]: p for p in index}
    results = []
    for entry in roster["members"]:
        accounts = entry["accounts"]
        sources = [indexed[a] for a in accounts if a in indexed]
        aliases = [entry["name"], *entry["aliases"], *(n for p in sources for n in p["aliases"])]
        results.append(dict(id=accounts[0], accounts=accounts, name=entry["name"],
                            aliases=list(dict.fromkeys(aliases)), count=sum(p["count"] for p in sources),
                            first=min((p["first"] for p in sources), default=""),
                            last=max((p["last"] for p in sources), default=""),
                            keywords=list(dict.fromkeys(w for p in sources for w in p["keywords"]))[:8]))
    return results


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    temp = Path(f.name)
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
