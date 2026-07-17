#!/usr/bin/env python3
"""thought-net: 二層思考グラフ生成（決定的パーサ・LLM不使用・stdlib のみ）

下層(layer=0): logbook + karpathy-wiki のノート、[[wikilink]] エッジ
上層(layer=1): logbook/projects/ の台帳 + ECC セッションの作業コンテキスト
垂直エッジ: projects 本文の [[link]]、ECC "Files Modified" のvault内パス

Phase 2:
  影響半径 = 直近 RECENT_DAYS 日に触れたノート集合から全エッジ BFS、
             各ノードに dist を付与（HTML 側で配色）
  解凍候補 = 凍結台帳ごとに隣接ノートの dist から近接スコアを算出、top-K 提示

PHI ガード: ラベルは filename / frontmatter title のみ（本文・ECC Tasks 欄は不使用）。
「取引記録」を含むパスは node/edge ごと drop。
"""
import json
import os
import re
import subprocess
import sys
import datetime
from pathlib import Path
from collections import Counter, deque

HOME = Path.home()
HERE = Path(__file__).parent

# ---- 設定（環境変数で上書き可能・デフォルトは作者の vault 構成）------------------
# 他の人が自分の Obsidian/Markdown vault で使えるよう、パス類は全て env で差し替え可能。
# THOUGHTNET_VAULTS="name1=/abs/path1:name2=/abs/path2"（コロン区切り・name=path）
# THOUGHTNET_PROJECTS_DIR / THOUGHTNET_ECC_DIR / THOUGHTNET_OUT / THOUGHTNET_PHI_BLOCK(カンマ区切り)
def _env_vaults():
    raw = os.environ.get("THOUGHTNET_VAULTS")
    if raw:
        out = {}
        for part in raw.split(":"):
            if "=" in part:
                name, path = part.split("=", 1)
                out[name.strip()] = Path(path).expanduser()
        if out:
            return out
    # デフォルト（作者の構成）。存在しない vault は scan_vaults が自然に無視する。
    return {"logbook": HOME / "logbook", "karpathy-wiki": HOME / "karpathy-wiki"}


DEMO = "--demo" in sys.argv
if DEMO:
    # 同梱の合成サンプル vault で動かす（実データ不要・OSS デモ用）
    VAULTS = {"sample": HERE / "sample-vault"}
    PROJECTS_DIR = HERE / "sample-vault" / "projects"
    ECC_DIR = HERE / "sample-vault" / ".sessions"
else:
    VAULTS = _env_vaults()
    PROJECTS_DIR = Path(os.environ.get(
        "THOUGHTNET_PROJECTS_DIR", str(next(iter(VAULTS.values())) / "projects"))).expanduser()
    ECC_DIR = Path(os.environ.get(
        "THOUGHTNET_ECC_DIR", str(HOME / ".claude" / "session-data"))).expanduser()

OUT_DIR = Path(os.environ.get("THOUGHTNET_OUT", str(HERE / "out"))).expanduser()
EXCLUDE_DIRS = {".obsidian", ".trash", ".git", "node_modules"}
# パスにこれを含むノードは一切載せない（機密フォルダの構造的遮断）。カンマ区切りで env 上書き可。
PHI_BLOCK = tuple(s for s in os.environ.get("THOUGHTNET_PHI_BLOCK", "取引記録").split(",") if s)
FROZEN_DAYS = int(os.environ.get("THOUGHTNET_FROZEN_DAYS", "21"))  # 休眠判定の日数
RECENT_DAYS = int(os.environ.get("THOUGHTNET_RECENT_DAYS", "14"))  # 影響半径の起点期間
THAW_TOP_K = 3     # 解凍候補の提示数
# UI 言語（デフォルト en＝OSS の対象は英語圏。作者は環境変数で ja に切替）
LANG = os.environ.get("THOUGHTNET_LANG", "en").lower()

# stdout メッセージの多言語辞書（表示は LANG で切替・graph の表示は template 側が担当）
MSG = {
    "en": {"nodes": "nodes", "know": "knowledge", "ledger": "projects", "ctx": "contexts",
           "live": "live windows", "links": "links", "reach": "influence radius: {r} nodes"
           " touched in last {d}d -> reached {a}/{k}",
           "thaw": "thaw candidate", "thaw_none": "thaw: none (no dormant project near recent work)",
           "mode": "thinking mode", "diverging": "diverging", "neutral": "steady",
           "converging": "converging", "meter": "{n} projects in {d}d / windows {c} linked, {u} unlinked",
           "inhead": "thin active projects (mostly in your head)", "out": "out"},
    "ja": {"nodes": "nodes", "know": "知識", "ledger": "台帳", "ctx": "文脈",
           "live": "🖥ライブ", "links": "links", "reach": "影響半径: 直近{d}日の起点 {r}ノート → 到達 {a}/{k}",
           "thaw": "🌱 解凍候補", "thaw_none": "🌱 解凍候補: なし（凍結台帳と最近の作業に近接なし）",
           "mode": "🧭 思考モード", "diverging": "発散", "neutral": "標準", "converging": "収束",
           "meter": "直近{d}日に触れた案件 {n}件 / 窓 {c}接続・{u}未帰属",
           "inhead": "📄 記録が薄い稼働案件（頭の中に偏りがち）", "out": "out"},
}
T = MSG.get(LANG, MSG["en"])

# 高精度の秘密/個人情報パターン（誤検出の少ないものだけ）。
# 電話・ID番号・APIキー・メールは高精度で墨消し。人名は誤検出が多いため対象外
# ＝そもそもラベルに本文/実名を載せない設計（filename/title のみ）で断つ。
REDACT_PATTERNS = [
    re.compile(r"\b0\d{1,4}-\d{1,4}-\d{3,4}\b"),          # 電話(ハイフン)
    re.compile(r"\b0\d{9,10}\b"),                          # 電話(連続)
    re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"),              # マイナンバー12桁
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[baprs])-[A-Za-z0-9_-]{6,}\b"),  # APIキー系
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                   # AWS
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),  # メール
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),                   # 長い16進トークン
]


def redact(text):
    """ラベル用の高精度墨消し。秘密/PHI パターンを ▓ で伏せる。"""
    if not text:
        return text
    for pat in REDACT_PATTERNS:
        text = pat.sub("▓▓", text)
    return text


WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
# vault内パス参照（実測: 23台帳中12件がこの書き方・wikilinkは2件のみ。2026-07-17）。
# ~/logbook/... / logbook/... / ~/karpathy-wiki/... 形式の .md 参照を拾う。
PATH_REF = re.compile(r"(?:~?/)?(logbook|karpathy-wiki)/([^\s)`'\"]+\.md)\b")
# markdown リンク [表示](パス)
MD_LINK = re.compile(r"\]\(([^)]+\.md)\)")
FM_TITLE = re.compile(r"^title:\s*(.+)$", re.M)
FM_FIELD = lambda k: re.compile(rf"^{k}:\s*(.+)$", re.M)
ECC_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def phi_ok(path_str: str) -> bool:
    return not any(b in path_str for b in PHI_BLOCK)


def frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[3:end]
    return ""


def scan_vaults():
    """全 .md を走査。projects/ 配下は思考層として別扱い。"""
    knowledge, projects = {}, {}
    for vname, vroot in VAULTS.items():
        if not vroot.is_dir():
            continue
        for p in vroot.rglob("*.md"):
            if any(part in EXCLUDE_DIRS for part in p.parts):
                continue
            if not phi_ok(str(p)):
                continue
            nid = f"{vname}/{p.relative_to(vroot)}"
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm = frontmatter(text)
            m = FM_TITLE.search(fm)
            label = m.group(1).strip().strip('"') if m else p.stem
            rec = {"id": nid, "label": label, "path": p, "text": text, "fm": fm}
            # 台帳判定: projects/ 直下で status フィールドを持つもの
            # （type: project 表記ゆれ・project: タイトル表記の実台帳を取りこぼさない）
            if p.parent == PROJECTS_DIR and FM_FIELD("status").search(fm):
                if not m:
                    pm = FM_FIELD("project").search(fm)
                    rec["label"] = pm.group(1).strip() if pm else p.stem
                projects[nid] = rec
            else:
                knowledge[nid] = rec
    return knowledge, projects


def basename_index(knowledge):
    """wikilink 解決用 basename → 候補 id リスト。"""
    idx = {}
    for nid, rec in knowledge.items():
        idx.setdefault(rec["path"].stem, []).append(nid)
    return idx


def make_resolver(idx):
    """曖昧な basename は 同vault → 同ディレクトリ → 辞書順 の優先で決定的に解決。"""
    stats = {"ambiguous": 0}

    def resolve(src_id, target):
        cands = idx.get(target.strip())
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        stats["ambiguous"] += 1
        src_vault = src_id.split("/", 1)[0]
        src_dir = src_id.rsplit("/", 1)[0]
        return min(cands, key=lambda c: (
            c.rsplit("/", 1)[0] != src_dir,       # 同ディレクトリ最優先
            c.split("/", 1)[0] != src_vault,      # 次に同vault
            c,                                     # 最後は辞書順（決定的）
        ))

    return resolve, stats


def dist_recent_projects(projects, today, days):
    """last_touch が直近 days 日以内の台帳 id 集合（発散/収束メーター用）。"""
    recent = set()
    for nid, rec in projects.items():
        lt = FM_FIELD("last_touch").search(rec["fm"])
        if not lt:
            continue
        try:
            d = datetime.date.fromisoformat(lt.group(1).strip())
        except ValueError:
            continue
        if (today - d).days <= days:
            recent.add(nid)
    return recent


def extract_path_links(text, nodeset):
    """本文中の vault パス参照 & markdown リンクから、実在する .md ノード id を返す。
    ユーザーは台帳を [[wikilink]] でなくパス参照で書くため（実測 12/23）、これが主経路。"""
    ids = set()
    for vault, rel in PATH_REF.findall(text):
        nid = f"{vault}/{rel}"
        if nid in nodeset:
            ids.add(nid)
    for raw in MD_LINK.findall(text):
        raw = raw.strip()
        # ~/logbook/... or /Users/.../logbook/... を vault相対 id へ
        for vname, vroot in VAULTS.items():
            marker = f"/{vname}/"
            if marker in raw:
                rel = raw.split(marker, 1)[1]
                nid = f"{vname}/{rel}"
                if nid in nodeset:
                    ids.add(nid)
    return ids


def parse_ecc():
    """ECC セッション tmp から (日付, Project名, Files Modified パス集合) を抽出。
    Tasks 欄（生発話）は構造上読まない。"""
    sessions = []
    if not ECC_DIR.is_dir():
        return sessions
    for f in sorted(ECC_DIR.glob("*-session.tmp")):
        dm = ECC_DATE.search(f.name)
        try:
            date = datetime.date.fromisoformat(dm.group(1)) if dm else None
        except ValueError:
            date = None
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # --demo の合成セッションはクローン先で動くようトークンを実パスへ展開
        if DEMO:
            text = text.replace("__SAMPLE_VAULT__", str(HERE / "sample-vault"))
        pm = re.search(r"^\*\*Project:\*\*\s*(.+)$", text, re.M)
        proj = pm.group(1).strip() if pm else "unknown"
        fm_sec = re.search(r"### Files Modified\n((?:- .+\n?)*)", text)
        if not fm_sec:
            continue
        paths = re.findall(r"^- (/\S+)$", fm_sec.group(1), re.M)
        if paths:
            sessions.append((date, proj, set(paths)))
    return sessions


def _etime_to_label(etime):
    """ps etime ([[dd-]hh:]mm:ss) → 「N日」「N時間」「N分」。"""
    days, hms = (etime.split("-") + [""])[:2] if "-" in etime else ("0", etime)
    parts = hms.split(":")
    hours = int(parts[0]) if len(parts) == 3 else 0
    if int(days):
        return f"{days}日"
    if hours:
        return f"{hours}時間"
    return f"{parts[-2]}分"


ADD_DIR = re.compile(r"--add-dir[= ]((?:/|~)[^\s]+)")


def live_sessions():
    """今リアルに開いている対話 claude セッションを tty 単位で列挙（ライブ層の素材）。

    セキュリティ境界（恒久ルール・2026-07-17 ユーザー合意・第3波で精緻化）:
      コマンドライン(command=)は **メモリ内でのみ** 走査し、抽出するのは
      `--add-dir <path>` のパスと cwd だけ。生の command 文字列・環境変数・
      会話ログ jsonl・ECC Tasks・画面内容は出力に一切残さない。
      `--api-key=...` 等の秘密は --add-dir 正規表現にマッチしないため構造的に漏れない。
      さらに抽出したパスは呼び出し側(main)で「既知の作業ディレクトリ」に一致した
      ものだけをラベル化する二段ガードで、未知の秘密名 dir は node に出ない。
    失敗は空で返す（fail-quiet: グラフ生成自体は止めない）。"""
    wins = []
    try:
        out = subprocess.run(["ps", "-axo", "pid=,tty=,etime=,command="],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return wins
    for ln in out.splitlines():
        f = ln.split(None, 3)
        if len(f) != 4 or not f[1].startswith("tty"):
            continue
        pid, tty, etime, command = f
        # 対話 claude 本体だけ拾う（bg-pty-host/daemon/spare/zsh ラッパーを除外）
        exe = command.split(None, 1)[0]
        if exe.rsplit("/", 1)[-1] != "claude" or "--bg-" in command or "daemon run" in command:
            continue
        # command は出力に残さない。--add-dir のパスだけ抽出（秘密は構造的に非マッチ）
        add_dirs = [str(Path(p).expanduser()) for p in ADD_DIR.findall(command)]
        cwd = None
        try:
            lo = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                                capture_output=True, text=True, timeout=10).stdout
            for l2 in lo.splitlines():
                if l2.startswith("n/") and phi_ok(l2):
                    cwd = l2[1:]
        except Exception:
            pass
        # tty デバイスの最終活動時刻（session 相関の材料。パス情報は含まない）
        tty_mtime = None
        try:
            tty_mtime = Path(f"/dev/{tty}").stat().st_mtime
        except OSError:
            pass
        wins.append({"tty": tty, "pid": pid, "age": _etime_to_label(etime),
                     "cwd": cwd, "add_dirs": add_dirs, "tty_mtime": tty_mtime,
                     "start": None})
    # プロセス起動時刻（jsonl birthtime との相関に使う）を一括取得
    if wins:
        try:
            out = subprocess.run(
                ["ps", "-o", "pid=,lstart=", "-p", ",".join(w["pid"] for w in wins)],
                capture_output=True, text=True, timeout=10,
                env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"}).stdout
            starts = {}
            for ln in out.splitlines():
                parts = ln.split(None, 1)
                if len(parts) != 2:
                    continue
                try:
                    starts[parts[0]] = datetime.datetime.strptime(
                        parts[1].strip(), "%a %b %d %H:%M:%S %Y").timestamp()
                except ValueError:
                    pass
            for w in wins:
                w["start"] = starts.get(w["pid"])
        except Exception:
            pass
    return wins


CLAUDE_PROJECTS = HOME / ".claude" / "projects"
SESSION_CWD = re.compile(r'"cwd"\s*:\s*"(/[^"]+)"')
SESSION_FILEPATH = re.compile(r'"file_path"\s*:\s*"(/[^"]+)"')


def recent_session_jsonls(days=30):
    """~/.claude/projects/*/*.jsonl の (path, birth, mtime) を列挙（メタデータのみ）。"""
    files = []
    cutoff = datetime.datetime.now().timestamp() - days * 86400
    if not CLAUDE_PROJECTS.is_dir():
        return files
    for f in CLAUDE_PROJECTS.glob("*/*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            continue
        birth = getattr(st, "st_birthtime", st.st_mtime)
        files.append((f, birth, st.st_mtime))
    return files


def match_windows_to_sessions(wins, files):
    """tty→session jsonl の時刻相関マッチ（窓・jsonl とも一意割当）。

    確度順の2判定のみ（誤リンクは無リンクより悪い＝許容窓は狭く）:
      1. birth 相関: jsonl 作成時刻がプロセス起動の [-2分, +15分] → 新規セッション
      2. 活動相関: tty デバイス最終活動と jsonl 最終追記が 300 秒以内 → 使用中の resume 窓
    どちらも greedy（Δ最小から確定）で 1:1 に割り当てる。外れた窓はハブのみ接続に留まる。
    """
    cands = []  # (priority, delta, win_idx, file_idx)
    for wi, w in enumerate(wins):
        start, tty_m = w.get("start"), w.get("tty_mtime")
        for fi, (f, birth, mtime) in enumerate(files):
            if start and mtime >= start - 60:
                d = birth - start
                # 自プロセスが作る jsonl の birth は必ず起動後（-5s は時計/秒精度の遊びのみ。
                # 直前セッションの jsonl を誤取りした実例: ttys008 Δ-1分 → 下限を締めた）
                if -5 <= d <= 900:
                    cands.append((0, abs(d), wi, fi))
                    continue
            if tty_m and abs(tty_m - mtime) <= 300:
                cands.append((1, abs(tty_m - mtime), wi, fi))
    cands.sort()
    assigned, used_w, used_f = {}, set(), set()
    for _, _, wi, fi in cands:
        if wi in used_w or fi in used_f:
            continue
        used_w.add(wi)
        used_f.add(fi)
        assigned[wi] = files[fi][0]
    return assigned


def session_work_paths(jsonl_path, tail_bytes=2_000_000, cap=400):
    """session jsonl から作業パスだけを **メモリ内で** 抽出（cwd と tool の file_path）。

    セキュリティ境界（live_sessions と同じ二段ガードの第1段）:
      会話本文・コマンド文字列は保持しない。抽出は絶対パスの正規表現2本のみで、
      返り値も頻度付きパス集合だけ。開示・リンクは呼び出し側の既知一致ガードを通る。
    """
    paths = Counter()
    try:
        with open(jsonl_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return paths
    for pat in (SESSION_CWD, SESSION_FILEPATH):
        for m in pat.findall(text):
            if phi_ok(m):
                paths[m] += 1
    return Counter(dict(paths.most_common(cap)))


def to_node_ids(paths, knowledge, projects):
    """絶対パス集合 → vault ノード id 集合（PHI drop 込み）。"""
    ids = set()
    for raw in paths:
        if not phi_ok(raw):
            continue
        p = Path(raw)
        for vname, vroot in VAULTS.items():
            try:
                rel = p.relative_to(vroot)
            except ValueError:
                continue
            tid = f"{vname}/{rel}"
            if tid in knowledge or tid in projects:
                ids.add(tid)
    return ids


def main():
    today = datetime.date.today()
    knowledge, projects = scan_vaults()
    idx = basename_index(knowledge)
    resolve, rstats = make_resolver(idx)

    nodes, links = [], []
    seen_links = set()

    def add_link(src, dst, kind):
        key = (src, dst, kind)
        if src != dst and key not in seen_links:
            seen_links.add(key)
            links.append({"source": src, "target": dst, "kind": kind})

    all_ids = set(knowledge) | set(projects)  # path参照の解決先集合

    # ---- 下層: 知識ノード + wikilink エッジ + パス参照エッジ
    dangling = Counter()
    for nid, rec in knowledge.items():
        nodes.append({"id": nid, "label": rec["label"], "layer": 0})
        for target in WIKILINK.findall(rec["text"]):
            tid = resolve(nid, target)
            if tid:
                add_link(nid, tid, "wiki")
            else:
                dangling[target.strip()] += 1
        for tid in extract_path_links(rec["text"], all_ids):
            add_link(nid, tid, "wiki")

    # ---- 上層: projects 台帳ノード + 垂直エッジ(a)
    frozen_projects = []
    for nid, rec in projects.items():
        fm = rec["fm"]
        status = (FM_FIELD("status").search(fm) or [None, ""])[1].strip()
        lt_raw = (FM_FIELD("last_touch").search(fm) or [None, ""])[1].strip()
        frozen = status == "not_started"
        try:
            lt = datetime.date.fromisoformat(lt_raw)
            frozen = frozen or (today - lt).days > FROZEN_DAYS
        except ValueError:
            pass
        node = {"id": nid, "label": rec["label"], "layer": 1,
                "kind": "project", "status": status,
                "last_touch": lt_raw, "frozen": frozen}
        nodes.append(node)
        if frozen:
            frozen_projects.append(node)
        for target in WIKILINK.findall(rec["text"]):
            tid = resolve(nid, target)
            if tid:
                add_link(nid, tid, "vertical")
        # 台帳はパス参照で書かれるのが主流（実測 12/23）→ これが繋がりの主経路
        for tid in extract_path_links(rec["text"], all_ids):
            add_link(nid, tid, "vertical")

    # ---- 上層: ECC 作業コンテキスト + 垂直エッジ(b) / 最近ノート集合
    sessions = parse_ecc()
    contexts = {}  # proj -> set(node ids) 全期間
    recent = set()  # 直近 RECENT_DAYS 日に触れた vault ノード
    for date, proj, paths in sessions:
        ids = to_node_ids(paths, knowledge, projects)
        contexts.setdefault(proj, set()).update(ids)
        if date and (today - date).days <= RECENT_DAYS:
            recent.update(i for i in ids if i in knowledge)
    for proj, ids in contexts.items():
        if not ids:
            continue
        cid = f"ecc/{proj}"
        nodes.append({"id": cid, "label": f"⚙ {proj}", "layer": 1,
                      "kind": "context", "frozen": False})
        for tid in ids:
            add_link(cid, tid, "vertical")

    # ---- ライブ層: 今開いている claude セッション（PM 直結の現在地・tty=窓1枚）
    #
    # 情報漏洩ガード（2026-07-17 red-team で cwd 生パス漏洩を発見し導入・第3波で拡張）:
    #   窓の候補ディレクトリ（cwd＋--add-dir）は生パスを出力に載せない。既知の作業場所
    #   （vault根／台帳 location:／台帳ファイル名=slug）に一致した窓だけ、その台帳名を
    #   ラベル開示＆リンク接続する。未知の秘密名 dir は tty＋経過時間のみ＝パス文字列ゼロ。
    #   窓の本当の作業対象は cwd(ホーム固定)でなく --add-dir にあるため両方を候補にする。
    live = [] if DEMO else live_sessions()  # デモは実プロセスを拾わず再現可能に
    # 既知dir → (開示ラベル, リンク先id)。パス完全一致＋名前一致の二系統。
    known_path = {}   # 絶対パス -> (label, node_id)
    known_name = {}   # ディレクトリ basename(slug) -> (label, node_id)
    for vname, vroot in VAULTS.items():
        known_path[str(vroot)] = (vname, None)
    for pid_, rec in projects.items():
        loc = FM_FIELD("location").search(rec["fm"])
        if loc:
            known_path[str(Path(loc.group(1).strip()).expanduser())] = (rec["label"], pid_)
        # 台帳ファイル名(slug)＋frontmatter slug をディレクトリ名の照合キーに
        slugs = {rec["path"].stem}
        sm = FM_FIELD("slug").search(rec["fm"])
        if sm:
            slugs.add(sm.group(1).strip())
        for s in slugs:
            known_name.setdefault(s, (rec["label"], pid_))

    def match_dir(path):
        """作業ディレクトリ → (開示ラベル, リンク先id) or None。生パスは返さない。
        完全一致 → パス階層のどれかの basename が既知 slug、の順で判定。"""
        if not path or not phi_ok(path):
            return None
        if path in known_path:
            return known_path[path]
        for part in Path(path).parts:
            if part in known_name:
                return known_name[part]
        return None

    # 起動ラッパーが記録した tty→起動dir マップ（~/.thought-net/live-map/<tty>）。
    # 素起動の窓は cwd=ホームで識別不能なため、起動ラッパーが残す「起動時の PWD」を
    # 最有力の候補にする。ps の live tty を真実源に、この map で起動dirを補う。
    launch_map = {}
    map_dir = HOME / ".thought-net" / "live-map"
    if map_dir.is_dir():
        for mf in map_dir.iterdir():
            try:
                launch_map[mf.name] = mf.read_text(encoding="utf-8").strip()
            except OSError:
                pass

    # tty→session jsonl の時刻相関（第4波・2026-07-17「全部繋げて」）。
    # 素起動（cwd=ホーム・map なし）の窓の作業対象は session transcript にしか無い。
    # transcript はメモリ内でパスだけ抽出し、既知一致ガード（match_dir / to_node_ids）
    # を通ったものだけ開示・リンクする＝生パス・会話内容は出力に載せない不変条件を維持。
    session_of = match_windows_to_sessions(live, recent_session_jsonls())

    live_ids = []
    brief = []  # 窓の棚卸し材料（--brief / session-start 用）
    for wi, w in enumerate(live):
        # 起動dir(map) + cwd + 全 --add-dir を候補に、既知一致を集める（生パスは保持しない）
        cands = [launch_map.get(w["tty"]), w["cwd"], *w.get("add_dirs", [])]
        matches = {}
        for cand in cands:
            m = match_dir(cand)
            if m:
                matches[m[1]] = m[0]  # node_id -> label（重複排除）
        # session 相関で得た作業パス → 台帳一致（ラベル開示）＋ vault ノート直リンク
        note_ids = set()
        if wi in session_of:
            wpaths = session_work_paths(session_of[wi])
            for p in wpaths:
                m = match_dir(p)
                if m:
                    matches.setdefault(m[1], m[0])
            note_ids = to_node_ids(wpaths, knowledge, projects)
        reveal = "・".join(dict.fromkeys(matches.values())) if matches else None
        label = f"🖥 {w['tty']}" + (f" {reveal}" if reveal else "") + f" ({w['age']})"
        lid = f"live/{w['tty']}"
        nodes.append({"id": lid, "label": label, "layer": 2, "kind": "live",
                      "frozen": False, "in_known": bool(matches or note_ids)})
        live_ids.append(lid)
        for link_to in matches:
            if link_to:
                add_link(lid, link_to, "live")
        for tid in sorted(note_ids):
            add_link(lid, tid, "live")

    # ---- ライブ層ハブ: 今開いている全窓を1つの「現在の作業面」に束ねる。
    #   cwd=ホームの素起動窓は既知プロジェクトに紐づけられず（＝上のガードで正しく
    #   リンク無し）散逸する。ハブはパスを一切明かさず（tty は既に開示済み）、窓を
    #   プロジェクトへ推測紐付けもしない＝セキュリティ境界を保ったまま live 層を連結
    #   する唯一の安全な結線。既知プロジェクト一致窓は加えてそのプロジェクトへも伸び、
    #   ハブのみ接続の窓＝未帰属（新規/発散）、ハブ＋台帳接続の窓＝継続作業、と読める。
    if live_ids:
        hub_id = "live/__now__"
        nodes.append({"id": hub_id, "label": "🖥 ライブ（今の窓）", "layer": 2,
                      "kind": "live-hub", "frozen": False})
        for lid in live_ids:
            add_link(hub_id, lid, "live-hub")

    # ---- Phase 2: 影響半径 BFS（全エッジ・recent を距離0に）
    adj = {}
    for l in links:
        adj.setdefault(l["source"], []).append(l["target"])
        adj.setdefault(l["target"], []).append(l["source"])
    dist = {nid: 0 for nid in recent}
    q = deque(recent)
    while q:
        cur = q.popleft()
        for nb in adj.get(cur, ()):
            if nb not in dist:
                dist[nb] = dist[cur] + 1
                q.append(nb)
    for n in nodes:
        if n["id"] in dist:
            n["dist"] = dist[n["id"]]

    # ---- Phase 2: 解凍候補スコア（凍結台帳 × 隣接ノートの recent 近接）
    label_of = {n["id"]: n["label"] for n in nodes}
    # セッション単位の共起: 同一セッションで台帳とノートを両方触った事実を
    # 関連の一次証拠とする（プロジェクト名集約ハブだと全台帳が等距離に縮退するため)
    session_ids = [to_node_ids(paths, knowledge, projects) for _, _, paths in sessions]
    # ハブ除去(IDF型): 台帳の過半と共起するノート(dashboard.md 等の機械更新物)は
    # 特定の台帳との関連を示さないため信号から除外する
    co_sets = {}
    for ids in session_ids:
        for pid in ids:
            if pid in projects:
                co_sets.setdefault(pid, set()).update(
                    x for x in ids if x in knowledge)
    note_freq = Counter(x for s in co_sets.values() for x in s)
    hub_notes = {x for x, c in note_freq.items()
                 if len(co_sets) >= 4 and c >= len(co_sets) / 2}
    # 一括保守セッション（台帳を4件以上同時に触る=移行・棚卸し）は思考の共起ではない
    bulk = [sum(1 for x in ids if x in projects) > 3 for ids in session_ids]
    thaw = []
    for fp in frozen_projects:
        if fp.get("status") in ("shelved", "infra"):
            continue  # 意図的休眠・インフラは催促しない
        contrib = {}  # knowledge id -> weight
        # 1ホップ: 台帳本文の [[link]] 先（現状ほぼ空だが将来のため重み1.0）
        for nb in adj.get(fp["id"], ()):
            if nb in knowledge:
                contrib[nb] = max(contrib.get(nb, 0), 1.0)
        # 共起: この台帳ファイルを触ったセッションが同時に触った知識ノート
        for ids, is_bulk in zip(session_ids, bulk):
            if is_bulk or fp["id"] not in ids:
                continue
            co = [x for x in ids if x in knowledge and x not in hub_notes]
            if not co:
                continue
            w = 1.0 / len(co)
            for x in co:
                contrib[x] = max(contrib.get(x, 0), w)
        scored = [(nid, dist[nid], w) for nid, w in contrib.items() if nid in dist]
        if not scored:
            continue
        score = sum(w / (1 + d) for _, d, w in scored)
        nearest = sorted(scored, key=lambda x: (x[1], -x[2]))[:2]
        thaw.append({
            "project": fp["label"], "id": fp["id"],
            "last_touch": fp.get("last_touch", ""),
            "score": round(score, 3),
            "via": [{"label": label_of[nid], "dist": d} for nid, d, _ in nearest],
        })
    thaw.sort(key=lambda t: -t["score"])
    thaw = thaw[:THAW_TOP_K]

    # degree 付与
    deg = Counter()
    for l in links:
        deg[l["source"]] += 1
        deg[l["target"]] += 1
    for n in nodes:
        n["degree"] = deg[n["id"]]

    # ---- 環境最適化① 真・空中プロジェクト検出（本文長ベース・2026-07-17）
    # 「接続0=頭の中」は測定アーティファクトだった（台帳はパス参照で書かれ、パーサが
    # [[wikilink]] しか読んでいなかった）。真の空中＝本文が実際に薄い稼働台帳。
    IN_HEAD_CHARS = 400
    in_head = []
    for nid, rec in projects.items():
        node = next(n for n in nodes if n["id"] == nid)
        if node.get("frozen"):
            continue  # 凍結は別枠（催促対象外）
        body = rec["text"][len(rec["fm"]) + 6:] if rec["fm"] else rec["text"]
        body_len = len(body.strip())
        node["body_len"] = body_len
        if body_len < IN_HEAD_CHARS:
            in_head.append((node["label"], body_len, deg[nid]))
    in_head.sort(key=lambda x: x[1])

    # ---- 環境最適化② 発散/収束メーター（レポート癖②の日次スカラー）
    recent_projs = [n for n in nodes if n.get("kind") == "project"
                    and n["id"] in dist_recent_projects(projects, today, RECENT_DAYS)]
    live_total = len(live)
    live_conn = sum(1 for n in nodes if n.get("kind") == "live" and n.get("in_known"))
    nproj = len(recent_projs)
    # 言語中立キー（template 側が表示言語に翻訳）
    mode = "converging" if nproj <= 3 else ("neutral" if nproj <= 6 else "diverging")
    focus_meter = {"mode": mode, "recent_projects": nproj,
                   "live_total": live_total, "live_connected": live_conn,
                   "live_unconnected": live_total - live_conn}

    # ---- 出力直前サニタイズ: 全ラベルの秘密/PHI 墨消し（表示される文字列すべて）
    for n in nodes:
        n["label"] = redact(n["label"])
    for t in thaw:
        t["project"] = redact(t["project"])
        for v in t["via"]:
            v["label"] = redact(v["label"])

    OUT_DIR.mkdir(exist_ok=True)
    graph = {"generated": today.isoformat(), "recent_days": RECENT_DAYS,
             "lang": LANG if LANG in MSG else "en",
             "recent_count": len(recent), "thaw": thaw,
             "focus_meter": focus_meter,
             "in_head": [{"label": redact(l), "body_len": b} for l, b, _ in in_head],
             "nodes": nodes, "links": links}
    (OUT_DIR / "graph.json").write_text(
        json.dumps(graph, ensure_ascii=False), encoding="utf-8")

    html = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    # <script> 埋込用に JSON をエスケープ（</script> タグ脱出と U+2028/2029 を封じる）。
    # JSON.parse で元の文字列値に戻るため、テンプレ側は必ず textContent 経由で描画すること。
    graph_json = (json.dumps(graph, ensure_ascii=False)
                  .replace("<", "\\u003c").replace(">", "\\u003e")
                  .replace("&", "\\u0026")
                  .replace(chr(0x2028), "\\u2028").replace(chr(0x2029), "\\u2029"))
    html = html.replace("/*__GRAPH_DATA__*/", graph_json)
    # vendored ライブラリを HTML へインライン（完全単一ファイル・オフライン閲覧）。
    # min.js に </script> が含まれる場合のみ安全側で外部ファイル同梱に落とす
    lib = (Path(__file__).parent / "vendor-3d-force-graph.min.js").read_text(encoding="utf-8")
    if "</script>" not in lib:
        html = html.replace('<script src="3d-force-graph.min.js"></script>',
                            "<script>\n" + lib + "\n</script>")
        (OUT_DIR / "3d-force-graph.min.js").unlink(missing_ok=True)
    else:
        (OUT_DIR / "3d-force-graph.min.js").write_text(lib, encoding="utf-8")
    (OUT_DIR / "thought-net.html").write_text(html, encoding="utf-8")

    # dangling wikilink レポート（グラフには載せない・棚卸し用）
    with (OUT_DIR / "dangling.txt").open("w", encoding="utf-8") as fh:
        fh.write(f"# 未解決 wikilink {sum(dangling.values())}箇所 / {len(dangling)}種 "
                 f"({today.isoformat()} 生成)\n")
        for target, c in dangling.most_common():
            fh.write(f"{c}\t{target}\n")

    k, pj = len(knowledge), len(projects)
    ctx = sum(1 for n in nodes if n.get("kind") == "context")
    lk = Counter(l["kind"] for l in links)
    reach = sum(1 for n in nodes if n["layer"] == 0 and "dist" in n)
    print(f"{T['nodes']}: {len(nodes)} ({T['know']} {k} / {T['ledger']} {pj} / "
          f"{T['ctx']} {ctx} / {T['live']} {len(live)})")
    print(f"{T['links']}: {len(links)} (wiki {lk['wiki']} / vertical {lk['vertical']} / "
          f"live {lk['live']} / live-hub {lk['live-hub']})")
    print(T["reach"].format(d=RECENT_DAYS, r=len(recent), a=reach, k=k))
    for t in thaw:
        via = " / ".join(f"{v['label']}(d{v['dist']})" for v in t["via"])
        print(f"{T['thaw']}: {t['project']} (score {t['score']}, {t['last_touch']}) <- {via}")
    if not thaw:
        print(T["thaw_none"])
    fm_ = focus_meter
    print(f"{T['mode']}: {T[fm_['mode']]} — " + T["meter"].format(
        d=RECENT_DAYS, n=fm_['recent_projects'],
        c=fm_['live_connected'], u=fm_['live_unconnected']))
    if in_head:
        top = " / ".join(f"{l}({b})" for l, b, _ in in_head[:5])
        print(f"{T['inhead']} ({len(in_head)}): {top}")
    print(f"dangling wikilinks: {sum(dangling.values())} (-> out/dangling.txt), "
          f"ambiguous resolved: {rstats['ambiguous']}")
    print(f"{T['out']}: {OUT_DIR / 'thought-net.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
