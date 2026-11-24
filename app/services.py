"""业务服务层：一轮核查的写入、重算与查询。"""
import hashlib
import json
import time

from .db import get_db, immediate_transaction
from .engine import PioEvent, simulate, validate_valid_lifetime
from .timeutil import TimeParseError, parse_utc


class ValidationError(Exception):
    """整批退回类错误，带逐行信息。"""

    def __init__(self, message, row_errors=None):
        super().__init__(message)
        self.message = message
        self.row_errors = row_errors or []


def _content_hash(router: str, received_at: int, valid_lifetime: int) -> str:
    payload = json.dumps(
        {"router": router, "received_at": received_at, "valid_lifetime": valid_lifetime},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 一轮核查

def get_round(conn):
    return conn.execute("SELECT * FROM rounds WHERE id = 1").fetchone()


def create_round(conn, snapshot_text, check_text):
    if get_round(conn) is not None:
        raise ValidationError("本轮核查已建立，快照/核查时刻不可修改；如需重来请清空数据")
    try:
        snapshot_at = parse_utc(snapshot_text)
        check_at = parse_utc(check_text)
    except TimeParseError as exc:
        raise ValidationError(str(exc))
    if check_at <= snapshot_at:
        raise ValidationError("核查时刻必须晚于快照时刻")
    conn.execute(
        "INSERT INTO rounds (id, snapshot_at, check_at, created_at) VALUES (1, ?, ?, ?)",
        (snapshot_at, check_at, int(time.time())),
    )
    return get_round(conn)


def list_terminals(conn):
    return conn.execute(
        "SELECT * FROM terminals ORDER BY name"
    ).fetchall()


def add_terminal(conn, name, snapshot_expiry_text):
    rnd = get_round(conn)
    if rnd is None:
        raise ValidationError("请先建立本轮核查（填写快照与核查 UTC 时刻）")
    name = (name or "").strip()
    if not name:
        raise ValidationError("终端名称不能为空")
    expiry = None
    if snapshot_expiry_text is not None and str(snapshot_expiry_text).strip():
        try:
            expiry = parse_utc(snapshot_expiry_text)
        except TimeParseError as exc:
            raise ValidationError(str(exc))
    try:
        cur = conn.execute(
            "INSERT INTO terminals (name, snapshot_expiry, created_at) VALUES (?, ?, ?)",
            (name, expiry, int(time.time())),
        )
    except Exception as exc:  # sqlite3.IntegrityError
        raise ValidationError(f"终端 {name} 已存在") from exc
    terminal_id = cur.lastrowid
    # 初始结论 = 快照基线
    conn.execute(
        """INSERT INTO conclusions (terminal_id, expiry_at, alive_at_check, source,
                                    last_pio_id, recomputed_at)
           VALUES (?, ?, ?, 'snapshot', NULL, ?)""",
        (
            terminal_id,
            expiry,
            1 if expiry is not None and expiry > rnd["check_at"] else 0,
            int(time.time()),
        ),
    )
    return terminal_id


# ---------------------------------------------------------------- PIO 补报

def _parse_batch_rows(raw_rows):
    """形式校验，返回 (规范化行, 行错误列表)。"""
    parsed, errors = [], []
    seen_keys = {}
    for idx, row in enumerate(raw_rows):
        label = f"第 {idx + 1} 行"
        errs = []
        name = (row.get("terminal") or "").strip()
        router = (row.get("router") or "").strip()
        if not name:
            errs.append("终端名称为空")
        if not router:
            errs.append("路由器为空")
        try:
            seq = int(str(row.get("seq", "")).strip())
            if seq < 0:
                errs.append("序号不能为负")
        except (ValueError, TypeError):
            seq = None
            errs.append(f"序号不是非负整数：{row.get('seq')!r}")
        received_at = None
        try:
            received_at = parse_utc(row.get("received_at"))
        except TimeParseError as exc:
            errs.append(str(exc))
        valid_lifetime = None
        try:
            valid_lifetime = validate_valid_lifetime(row.get("valid_lifetime"))
        except ValueError as exc:
            errs.append(str(exc))

        if errs:
            errors.append({"row": idx + 1, "terminal": name, "errors": errs})
            continue
        key = (name, seq)
        if key in seen_keys:
            errors.append(
                {"row": idx + 1, "terminal": name,
                 "errors": [f"批次内 (终端, 序号)=({name}, {seq}) 重复，首次出现在第 {seen_keys[key]} 行"]}
            )
            continue
        seen_keys[key] = idx + 1
        parsed.append(
            {
                "row": idx + 1,
                "terminal": name,
                "seq": seq,
                "router": router,
                "received_at": received_at,
                "valid_lifetime": valid_lifetime,
                "content_hash": _content_hash(router, received_at, valid_lifetime),
            }
        )
    return parsed, errors


def submit_pio_batch(db_path, raw_rows):
    """原子地接收一批 PIO 补报并重算受影响终端。

    * 任何一行形式错误、同号异内容冲突 → 整批退回，库内原结果不变；
    * 原样重报 → 跳过、不重算；
    * 只有确实插入了新记录的终端才重算（重放该终端全部通告）。
    """
    if not raw_rows:
        raise ValidationError("提交内容为空")

    conn = get_db(db_path)
    try:
        with immediate_transaction(conn):
            rnd = get_round(conn)
            if rnd is None:
                raise ValidationError("请先建立本轮核查（填写快照与核查 UTC 时刻）")

            parsed, row_errors = _parse_batch_rows(raw_rows)
            if row_errors:
                raise ValidationError("存在形式错误，整批退回", row_errors)

            # 时间窗口与终端存在性
            terminal_cache = {}
            for p in parsed:
                t = conn.execute(
                    "SELECT * FROM terminals WHERE name = ?", (p["terminal"],)
                ).fetchone()
                if t is None:
                    row_errors.append(
                        {"row": p["row"], "terminal": p["terminal"],
                         "errors": ["终端不存在，请先在快照页登记"]}
                    )
                else:
                    terminal_cache[p["terminal"]] = t
                if t is not None and not (rnd["snapshot_at"] < p["received_at"] <= rnd["check_at"]):
                    row_errors.append(
                        {"row": p["row"], "terminal": p["terminal"],
                         "errors": [
                             "接收时刻必须晚于快照时刻且不晚于核查时刻"
                             f"（快照 {rnd['snapshot_at']} / 核查 {rnd['check_at']} 纪元秒）"
                         ]}
                    )
            if row_errors:
                raise ValidationError("存在业务校验错误，整批退回", row_errors)

            # 同号冲突 / 原样重报
            new_rows, duplicate_count, conflict_errors = [], 0, []
            for p in parsed:
                t = terminal_cache[p["terminal"]]
                existing = conn.execute(
                    "SELECT * FROM pio_records WHERE terminal_id = ? AND seq = ?",
                    (t["id"], p["seq"]),
                ).fetchone()
                if existing is not None:
                    if existing["content_hash"] == p["content_hash"]:
                        duplicate_count += 1  # 原样重报：幂等跳过
                        continue
                    conflict_errors.append(
                        {"row": p["row"], "terminal": p["terminal"], "errors": [
                            f"序号 {p['seq']} 已存在但内容不同"
                            f"（库内：路由器={existing['router']}, 接收={existing['received_at']}, "
                            f"寿命={existing['valid_lifetime']}）；同号异内容，整批退回"
                        ]}
                    )
                    continue
                new_rows.append((t, p))
            if conflict_errors:
                raise ValidationError("存在同号异内容冲突，整批退回", conflict_errors)

            now = int(time.time())
            inserted_ids = []
            affected_terminal_ids = set()
            for t, p in new_rows:
                cur = conn.execute(
                    """INSERT INTO pio_records
                       (terminal_id, seq, router, received_at, valid_lifetime, content_hash, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (t["id"], p["seq"], p["router"], p["received_at"],
                     p["valid_lifetime"], p["content_hash"], now),
                )
                inserted_ids.append(cur.lastrowid)
                affected_terminal_ids.add(t["id"])

            recomputed = []
            for terminal_id in sorted(affected_terminal_ids):
                _recompute_terminal(conn, terminal_id, rnd)
                tname = conn.execute("SELECT name FROM terminals WHERE id = ?", (terminal_id,)).fetchone()
                recomputed.append(tname["name"])

            return {
                "inserted": len(inserted_ids),
                "duplicates_skipped": duplicate_count,
                "recomputed": recomputed,
            }
    finally:
        conn.close()


def _recompute_terminal(conn, terminal_id, rnd):
    """重放该终端的全部 PIO（含历史记录），落结论与逐条依据。"""
    t = conn.execute("SELECT * FROM terminals WHERE id = ?", (terminal_id,)).fetchone()
    records = conn.execute(
        "SELECT * FROM pio_records WHERE terminal_id = ? ORDER BY received_at, seq",
        (terminal_id,),
    ).fetchall()
    events = [
        PioEvent(
            seq=r["seq"],
            received_at=r["received_at"],
            valid_lifetime=r["valid_lifetime"],
            router=r["router"],
            row_id=r["id"],
        )
        for r in records
    ]
    result = simulate(events, t["snapshot_expiry"])

    conn.execute("DELETE FROM pio_decisions WHERE pio_id IN (SELECT id FROM pio_records WHERE terminal_id = ?)",
                 (terminal_id,))
    for step in result.steps:
        conn.execute(
            """INSERT INTO pio_decisions
               (pio_id, rule, changed, expiry_before, expiry_after, remaining_before, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (step.row_id, step.rule, 1 if step.changed else 0, step.expiry_before,
             step.expiry_after, step.remaining_before, step.detail),
        )

    expiry = result.expiry
    alive = 1 if expiry is not None and expiry > rnd["check_at"] else 0
    last_pio_id = result.last_change.row_id if result.last_change is not None else None
    conn.execute(
        """INSERT INTO conclusions (terminal_id, expiry_at, alive_at_check, source, last_pio_id, recomputed_at)
           VALUES (?, ?, ?, 'pio', ?, ?)
           ON CONFLICT(terminal_id) DO UPDATE SET
             expiry_at=excluded.expiry_at,
             alive_at_check=excluded.alive_at_check,
             source=excluded.source,
             last_pio_id=excluded.last_pio_id,
             recomputed_at=excluded.recomputed_at""",
        (terminal_id, expiry, alive, last_pio_id, int(time.time())),
    )


# ---------------------------------------------------------------- 查询

def results_overview(conn):
    rnd = get_round(conn)
    rows = conn.execute(
        """SELECT t.id, t.name, t.snapshot_expiry,
                  c.expiry_at, c.alive_at_check, c.source, c.last_pio_id,
                  p.router AS last_router, p.seq AS last_seq,
                  p.received_at AS last_received_at, p.valid_lifetime AS last_valid_lifetime,
                  (SELECT COUNT(*) FROM pio_records pr WHERE pr.terminal_id = t.id) AS pio_count
           FROM terminals t
           LEFT JOIN conclusions c ON c.terminal_id = t.id
           LEFT JOIN pio_records p ON p.id = c.last_pio_id
           ORDER BY t.name""",
    ).fetchall()
    return rnd, rows


def terminal_detail(conn, terminal_id):
    t = conn.execute("SELECT * FROM terminals WHERE id = ?", (terminal_id,)).fetchone()
    if t is None:
        return None
    rnd = get_round(conn)
    records = conn.execute(
        """SELECT pr.*, d.rule, d.changed, d.expiry_before, d.expiry_after,
                  d.remaining_before, d.detail
           FROM pio_records pr
           LEFT JOIN pio_decisions d ON d.pio_id = pr.id
           WHERE pr.terminal_id = ?
           ORDER BY pr.received_at, pr.seq""",
        (terminal_id,),
    ).fetchall()
    conclusion = conn.execute(
        "SELECT * FROM conclusions WHERE terminal_id = ?", (terminal_id,)
    ).fetchone()
    return {"round": rnd, "terminal": t, "records": records, "conclusion": conclusion}


def reset_all(db_path):
    conn = get_db(db_path)
    try:
        with immediate_transaction(conn):
            for table in ("pio_decisions", "conclusions", "pio_records", "terminals", "rounds"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM sqlite_sequence")
    finally:
        conn.close()
